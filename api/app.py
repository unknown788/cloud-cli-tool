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
  WS   /ws/{job_id}              — live log streaming over WebSocket

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
import asyncio
import functools
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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

# Serve the web dashboard from the static/ directory.
# Mount AFTER all API routes so /static/* doesn't shadow API paths.
_static_dir = Path(__file__).parent.parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

STATE_FILE = "state.json"


# ---------------------------------------------------------------------------
# Root redirect → dashboard
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    """Redirect / → /static/dashboard.html for convenience."""
    return RedirectResponse(url="/static/dashboard.html")


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
# WebSocket — live log streaming
# ---------------------------------------------------------------------------

@app.websocket("/ws/{job_id}")
async def websocket_logs(websocket: WebSocket, job_id: str):
    """
    Stream real-time log lines for a running (or completed) job.

    Protocol — every message is a JSON frame:

      {"type": "log",      "data": "<log line>"}
        Emitted for each log line as the provider produces it.

      {"type": "status",   "data": "running"|"succeeded"|"failed"}
        Emitted when job status transitions.  Sent once at connect
        (current state) and again when the job finishes.

      {"type": "result",   "data": { ...state dict... }}
        Emitted on provision success — contains the public IP etc.
        Null for deploy/destroy jobs.

      {"type": "error",    "data": "<error message>"}
        Emitted if the job fails, then the connection closes.

      {"type": "done"}
        Final frame — no more data.  Client should close the socket.

      {"type": "ping"}
        Heartbeat sent every 15 s while the job is still running.
        Keeps the connection alive through proxies / load balancers.

    Late-join behaviour:
      If a client connects after the job has already produced log lines
      (or even after it has finished), all historical logs are replayed
      first, then live streaming continues (or "done" is sent immediately
      if the job is already complete).

    Threading bridge:
      The provider runs in a daemon thread and writes to job.log_queue
      (a stdlib queue.Queue).  This async handler must NOT block the
      event loop, so it reads from the queue using run_in_executor(),
      which offloads the blocking queue.get(timeout=…) call to a thread
      pool thread — leaving the event loop free to serve other requests.
    """
    await websocket.accept()

    job = job_store.get(job_id)
    if not job:
        await websocket.send_text(
            json.dumps({"type": "error", "data": f"Job '{job_id}' not found."})
        )
        await websocket.close(code=4004)
        return

    loop = asyncio.get_event_loop()

    async def send(frame: dict) -> None:
        """Send a JSON frame, silently swallow disconnect errors."""
        try:
            await websocket.send_text(json.dumps(frame))
        except WebSocketDisconnect:
            pass

    # ------------------------------------------------------------------
    # 1. Replay history — so late-joining clients see everything already
    #    emitted before they connected.
    #
    #    We snapshot job.logs under the assumption that the background
    #    thread may still be appending.  We record how many lines we
    #    replayed (history_count) so we know how many items to skip
    #    from the live queue below — those items are duplicates.
    # ------------------------------------------------------------------
    history_snapshot = list(job.logs)   # atomic snapshot (GIL protects list copy)
    history_count = len(history_snapshot)
    for line in history_snapshot:
        await send({"type": "log", "data": line})

    # Send current status so the client knows where we are
    await send({"type": "status", "data": job.status.value})

    # ------------------------------------------------------------------
    # 2. If the job is already finished, send result/error + done frame
    #    and close immediately.  No need to wait on the queue.
    # ------------------------------------------------------------------
    if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        if job.result:
            await send({"type": "result", "data": job.result})
        if job.error:
            await send({"type": "error", "data": job.error})
        await send({"type": "done"})
        await websocket.close()
        return

    # ------------------------------------------------------------------
    # 3. Job is still running — drain the queue in real time.
    #
    #    The queue contains ALL lines the background thread has written
    #    since the job started — including the ones we just replayed from
    #    job.logs.  We skip the first `history_count` items so we don't
    #    send duplicates, then stream everything new as it arrives.
    #
    #    We use run_in_executor() to call queue.get(timeout=15) in a
    #    thread pool.  This blocks that pool thread for up to 15 s while
    #    the event loop remains free.  If the timeout fires we send a
    #    heartbeat ping and retry — this keeps proxies alive.
    # ------------------------------------------------------------------
    def _blocking_get() -> object:
        """
        Called in a thread pool.  Blocks until a log line (str) or the
        sentinel (None) arrives, or times out after 15 s.
        Returns the item, or raises queue.Empty on timeout.
        """
        return job.log_queue.get(timeout=15)

    skipped = 0   # count of already-replayed items we consume but don't send

    try:
        while True:
            try:
                item = await loop.run_in_executor(None, _blocking_get)
            except asyncio.CancelledError:
                # Client disconnected while we were waiting — clean exit.
                break
            except Exception:
                # queue.Empty (15 s timeout) — send heartbeat and retry.
                await send({"type": "ping"})
                continue

            if item is None:
                # Sentinel — background thread is finished.
                break

            if skipped < history_count:
                # This item was already replayed from job.logs — discard.
                skipped += 1
                continue

            await send({"type": "log", "data": item})

    except WebSocketDisconnect:
        # Client closed the socket mid-stream — nothing to do.
        return

    # ------------------------------------------------------------------
    # 4. Job finished while we were streaming.  Send final frames.
    # ------------------------------------------------------------------
    await send({"type": "status", "data": job.status.value})

    if job.result:
        await send({"type": "result", "data": job.result})
    if job.error:
        await send({"type": "error", "data": job.error})

    await send({"type": "done"})
    await websocket.close()
