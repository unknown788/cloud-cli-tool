"""
api/app.py

FastAPI application for the CloudLaunch Platform.

Endpoints:
  GET  /healthz                  — liveness probe (no auth needed)
  GET  /plan                     — preview resources, zero cloud calls
  POST /provision                — async VM provisioning  → 202 + job_id
  POST /deploy                   — async app deployment   → 202 + job_id
  POST /destroy                  — async teardown         → 202 + job_id
  GET  /status                   — real-time VM state from cloud API
  GET  /jobs                     — list all jobs
  GET  /jobs/{job_id}            — full job detail (logs + status)
  WS   /ws/{job_id}              — Phase 5: live log streaming (stub here)

Design decisions:
  - All mutations are async (background thread) and return 202 immediately.
    This is critical: provisioning takes 3-5 minutes — a synchronous HTTP
    handler would time out every reverse proxy and load balancer on the planet.
  - The `log=` callable hook (established in Phase 1) is the bridge between
    the synchronous provider code and the async WebSocket stream.
  - State is read from / written to state.json on disk, same as the CLI.
    This means the CLI and API are always in sync — no separate DB needed
    for a single-user tool.
  - CORS is open (*) for development. Tighten in production.
"""

import json
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from providers import get_provider, ProvisionConfig
from api.schemas import (
    JobStatus,
    ProvisionRequest,
    DeployRequest,
    DestroyRequest,
    JobResponse,
    JobDetailResponse,
    VMStatusResponse,
    PlanResponse,
    PlanResource,
)
from api.jobs import job_store, launch_job

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CloudLaunch API",
    description=(
        "REST + WebSocket API for the CloudLaunch Platform.\n\n"
        "Provision Azure VMs, deploy containerised apps, and stream "
        "real-time logs — all over HTTP."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)

STATE_FILE = "state.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Load state.json or raise 404 with a clear message."""
    if not os.path.exists(STATE_FILE):
        raise HTTPException(
            status_code=404,
            detail="No infrastructure state found. Run POST /provision first.",
        )
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def _save_state(data: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=4)


def _job_response(job) -> JobResponse:
    return JobResponse(job_id=job.job_id, status=job.status, message=job.message)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["Meta"], summary="Liveness probe")
def healthz():
    """Returns 200 OK. Used by load balancers and Docker HEALTHCHECK."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Plan  (zero cloud calls)
# ---------------------------------------------------------------------------

@app.get(
    "/plan",
    response_model=PlanResponse,
    tags=["Operations"],
    summary="Preview resources without making any cloud calls",
)
def get_plan(
    provider: str = "azure",
    location: str = "southeastasia",
    vm_name:  str = "cloudlaunch-vm",
    resource_group: str = "cloudlaunch-rg",
    admin_username: str = "azureuser",
):
    """
    Returns the list of resources that WOULD be created by /provision.
    Instant response — no Azure credentials needed.
    """
    try:
        p = get_provider(provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    config = ProvisionConfig(
        vm_name=vm_name,
        location=location,
        admin_username=admin_username,
        ssh_key_path=str(Path.home() / ".ssh" / "id_rsa.pub"),
        resource_group=resource_group,
    )
    raw = p.get_plan(config)
    return PlanResponse(
        provider=provider,
        location=location,
        vm_size="Standard_B1s",
        resources=[PlanResource(**r) for r in raw],
    )


# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------

@app.post(
    "/provision",
    response_model=JobResponse,
    status_code=202,
    tags=["Operations"],
    summary="Provision a complete VM stack (async)",
)
def provision(req: ProvisionRequest):
    """
    Kicks off VM provisioning in a background thread.

    Returns immediately with a `job_id`. The client should:
    1. Open `WS /ws/{job_id}` to stream real-time logs.
    2. Poll `GET /jobs/{job_id}` to check completion.

    On success the provisioned VM state (including public IP) is available
    in `GET /jobs/{job_id}` → `result` field.
    """
    try:
        p = get_provider(req.provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ssh_key_path = req.ssh_key_path.replace("~", str(Path.home()))
    config = ProvisionConfig(
        vm_name=req.vm_name,
        location=req.location,
        admin_username=req.admin_username,
        ssh_key_path=ssh_key_path,
        resource_group=req.resource_group,
    )

    def _provision_and_save(log=print):
        """Thin wrapper: provision + persist state to disk."""
        state = p.provision(config, log=log)
        _save_state(state)
        return state

    job = launch_job("provision", _provision_and_save)
    return _job_response(job)


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

@app.post(
    "/deploy",
    response_model=JobResponse,
    status_code=202,
    tags=["Operations"],
    summary="Deploy the containerised app to the provisioned VM (async)",
)
def deploy(req: DeployRequest):
    """
    Builds and runs the Docker container on the provisioned VM.
    Requires a prior successful POST /provision (reads state.json).
    """
    state = _load_state()
    try:
        p = get_provider(req.provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = launch_job("deploy", p.deploy, state)
    return _job_response(job)


# ---------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------

@app.post(
    "/destroy",
    response_model=JobResponse,
    status_code=202,
    tags=["Operations"],
    summary="Tear down all cloud resources (async)",
)
def destroy(req: DestroyRequest):
    """
    Deletes the entire resource group and all cloud resources.
    Also removes state.json on success.

    ⚠ This is irreversible.
    """
    state = _load_state()
    try:
        p = get_provider(req.provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _destroy_and_cleanup(log=print):
        p.destroy(state, log=log)
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)

    job = launch_job("destroy", _destroy_and_cleanup)
    return _job_response(job)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get(
    "/status",
    response_model=VMStatusResponse,
    tags=["Operations"],
    summary="Query real-time VM status from the cloud API",
)
def get_status(provider: str = "azure"):
    """
    Calls the cloud API to get the current power state of the VM.
    Returns normalised VMStatusResponse regardless of provider.
    """
    state = _load_state()
    try:
        p = get_provider(provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        vm_status = p.get_status(state)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return VMStatusResponse(
        vm_name=vm_status.vm_name,
        provider=vm_status.provider,
        state=vm_status.state,
        public_ip=vm_status.public_ip,
        location=vm_status.location,
        vm_size=vm_status.vm_size,
        os_disk_size_gb=vm_status.os_disk_size_gb,
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@app.get(
    "/jobs",
    response_model=List[JobDetailResponse],
    tags=["Jobs"],
    summary="List all jobs",
)
def list_jobs():
    """Returns all jobs (completed and in-progress), newest first."""
    return [
        JobDetailResponse(
            job_id=j.job_id,
            status=j.status,
            message=j.message,
            logs=j.logs,
            error=j.error,
            result=j.result,
        )
        for j in job_store.all()
    ]


@app.get(
    "/jobs/{job_id}",
    response_model=JobDetailResponse,
    tags=["Jobs"],
    summary="Get full details for a single job",
)
def get_job(job_id: str):
    """
    Returns the full job record including all accumulated log lines.

    Poll this endpoint after opening the WebSocket to confirm final status.
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return JobDetailResponse(
        job_id=job.job_id,
        status=job.status,
        message=job.message,
        logs=job.logs,
        error=job.error,
        result=job.result,
    )


# ---------------------------------------------------------------------------
# WebSocket stub (Phase 5 will replace this)
# ---------------------------------------------------------------------------

@app.websocket("/ws/{job_id}")
async def websocket_logs(websocket, job_id: str):
    """
    Phase 5 placeholder.
    Real implementation in Phase 5: drain job.log_queue in an async loop.
    """
    from fastapi import WebSocket
    await websocket.accept()
    await websocket.send_text(
        f'{{"type":"info","data":"WebSocket streaming coming in Phase 5. '
        f'Poll GET /jobs/{job_id} for status."}}'
    )
    await websocket.close()
