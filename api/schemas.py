"""
api/schemas.py

Pydantic models for all API request and response bodies.

Design philosophy:
  - Request models validate and document what the caller must send.
  - Response models are the single source of truth for what we return.
  - We never expose raw Azure SDK objects — always these normalised shapes.
  - JobResponse is the central type: every mutation endpoint returns one.
    The frontend only needs to know one shape and can poll /jobs/{job_id}.
"""

from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Job state machine
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    """
    Lifecycle of an async job (provision / deploy / destroy).

    Transitions (happy path):
        pending → running → succeeded

    Transitions (failure):
        pending → running → failed

    pending   : Job accepted, background thread not yet started.
    running   : Background thread is actively calling the cloud provider.
    succeeded : Provider method returned without raising an exception.
    failed    : Provider method raised; error message stored in Job.error.
    """
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCEEDED = "succeeded"
    FAILED    = "failed"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ProvisionRequest(BaseModel):
    """Body for POST /provision — mirrors ProvisionConfig dataclass."""
    provider:       str = Field("azure",          description="Cloud provider: azure | aws")
    resource_group: str = Field("cloudlaunch-rg", description="Resource group / project namespace")
    location:       str = Field("southeastasia",  description="Cloud region slug")
    vm_name:        str = Field("cloudlaunch-vm", description="VM hostname")
    admin_username: str = Field("azureuser",      description="Linux admin username")
    ssh_key_path:   str = Field("~/.ssh/id_rsa.pub", description="Absolute path to SSH public key on the server")

    class Config:
        json_schema_extra = {
            "example": {
                "provider": "azure",
                "resource_group": "my-rg",
                "location": "eastus",
                "vm_name": "my-vm",
                "admin_username": "azureuser",
                "ssh_key_path": "/home/user/.ssh/id_rsa.pub",
            }
        }


class DeployRequest(BaseModel):
    """Body for POST /deploy — provider tells us which SDK to use."""
    provider: str = Field("azure", description="Cloud provider: azure | aws")


class DestroyRequest(BaseModel):
    """Body for POST /destroy — provider tells us which SDK to use."""
    provider: str = Field("azure", description="Cloud provider: azure | aws")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class JobResponse(BaseModel):
    """
    Returned by every mutation endpoint (provision, deploy, destroy).

    The client should:
      1. Store job_id.
      2. Open WS ws://<host>/ws/{job_id} to stream real-time logs.
      3. Poll GET /jobs/{job_id} to check status after WS closes.
    """
    job_id:  str       = Field(..., description="UUID identifying this async operation")
    status:  JobStatus = Field(..., description="Current lifecycle state")
    message: str       = Field(..., description="Human-readable summary of current state")


class JobDetailResponse(BaseModel):
    """
    Full job record returned by GET /jobs/{job_id}.
    Superset of JobResponse — includes logs and result state.
    """
    job_id:   str            = Field(..., description="UUID")
    status:   JobStatus      = Field(..., description="Current lifecycle state")
    message:  str            = Field(..., description="Human-readable summary")
    logs:     List[str]      = Field(default_factory=list, description="All log lines emitted so far")
    error:    Optional[str]  = Field(None, description="Exception message if status=failed")
    result:   Optional[dict] = Field(None, description="State dict returned by provision(); null for other operations")


class VMStatusResponse(BaseModel):
    """Returned by GET /status — mirrors the VMStatus dataclass."""
    vm_name:        str
    provider:       str
    state:          str            # "running" | "stopped" | "deallocated" | "unknown"
    public_ip:      Optional[str]
    location:       str
    vm_size:        str
    os_disk_size_gb: Optional[int]


class PlanResource(BaseModel):
    """One row in the plan table — mirrors get_plan() dict items."""
    resource: str
    name:     str
    type:     str
    detail:   str


class PlanResponse(BaseModel):
    """Returned by GET /plan — zero cloud calls, instant response."""
    provider:  str
    location:  str
    vm_size:   str
    resources: List[PlanResource]
