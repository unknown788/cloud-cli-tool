"""
api/jobs.py

In-memory job store and the background task runner.

Design:
  - Every mutation (provision/deploy/destroy) creates a Job and runs in a
    daemon thread so the HTTP response returns immediately (202 Accepted).
  - Each Job owns a Queue[str] that the provider writes to via `log=queue.put`.
  - Phase 5's WebSocket endpoint drains that same queue in real time.
  - JobStore is a plain dict protected by a threading.Lock — no database
    needed because jobs are ephemeral (one server process, one user).

Thread safety model:
  - All Job mutations go through JobStore methods (acquire lock before write).
  - Readers (WebSocket drain, GET /jobs/{id}) read without a lock because:
      * Python's GIL makes dict reads atomic for CPython.
      * Stale reads are acceptable for a log streaming use-case.
  - The Queue itself is thread-safe by design (stdlib).
"""

import threading
import queue
import uuid
from typing import Optional, List, Dict
from datetime import datetime, timezone

from api.schemas import JobStatus


# ---------------------------------------------------------------------------
# Job data class
# ---------------------------------------------------------------------------

class Job:
    """
    Represents one async cloud operation.

    Attributes:
        job_id      : UUID string, stable for the lifetime of the operation.
        operation   : Human label — "provision" | "deploy" | "destroy".
        status      : Current JobStatus enum value.
        log_queue   : thread-safe Queue; provider writes here, WS drains it.
        logs        : Accumulated list of all log lines (for GET /jobs/{id}).
        error       : Set on failure — the exception's str().
        result      : Set on provision success — the state dict (public IP etc.)
        created_at  : ISO-8601 UTC timestamp for debugging / ordering.
        caller_ip   : IP of the HTTP client that created this job (for
                      concurrency-slot release on completion).
    """

    def __init__(self, operation: str, caller_ip: str = "unknown"):
        self.job_id:    str            = str(uuid.uuid4())
        self.operation: str            = operation
        self.status:    JobStatus      = JobStatus.PENDING
        self.log_queue: queue.Queue    = queue.Queue()
        self.logs:      List[str]      = []
        self.error:     Optional[str]  = None
        self.result:    Optional[dict] = None
        self.created_at: str           = datetime.now(timezone.utc).isoformat()
        self.caller_ip: str            = caller_ip

    # Convenience properties for response serialisation
    @property
    def message(self) -> str:
        status_messages = {
            JobStatus.PENDING:   f"{self.operation} job queued.",
            JobStatus.RUNNING:   f"{self.operation} is running…",
            JobStatus.SUCCEEDED: f"{self.operation} completed successfully.",
            JobStatus.FAILED:    f"{self.operation} failed: {self.error}",
        }
        return status_messages[self.status]


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

class JobStore:
    """
    Thread-safe in-memory store for all active/completed jobs.
    Singleton — imported as `job_store` at module level.
    """

    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, operation: str, caller_ip: str = "unknown") -> Job:
        """Create a new Job, register it, and return it."""
        job = Job(operation, caller_ip=caller_ip)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        """Return the Job or None if not found."""
        return self._jobs.get(job_id)

    def all(self) -> List[Job]:
        """Return all jobs, newest first."""
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)


# Module-level singleton — imported everywhere in the API layer.
job_store = JobStore()


# ---------------------------------------------------------------------------
# Background task runner
# ---------------------------------------------------------------------------

def _run_job(job: Job, fn, *args, **kwargs) -> None:
    """
    Target function for background threads.

    1. Marks job as RUNNING.
    2. Calls fn(*args, **kwargs) with log=_log injected.
    3. On success: marks SUCCEEDED, stores result (if provision).
    4. On failure: marks FAILED, stores exception message.
    5. Always sends a sentinel None to log_queue so the WebSocket
       consumer knows the stream is finished.
    6. Releases the concurrency slot so the next caller can proceed.

    The `log=` callable captures each line into both:
      - job.log_queue  (for live WebSocket streaming)
      - job.logs       (for GET /jobs/{id} history)
    """
    # Import here to avoid circular import (middleware → jobs → middleware)
    from api.middleware import release_concurrency_slot

    def _log(line: str) -> None:
        job.logs.append(line)
        job.log_queue.put(line)

    job.status = JobStatus.RUNNING
    try:
        result = fn(*args, log=_log, **kwargs)
        job.result = result          # None for deploy/destroy, dict for provision
        job.status = JobStatus.SUCCEEDED
    except Exception as exc:
        job.error = str(exc)
        job.status = JobStatus.FAILED
    finally:
        # Sentinel value: WebSocket consumer stops when it receives None
        job.log_queue.put(None)
        # Release the concurrency reservation made at HTTP request time
        release_concurrency_slot(job.caller_ip)


def launch_job(operation: str, fn, *args, caller_ip: str = "unknown", **kwargs) -> Job:
    """
    Create a Job and immediately start a daemon thread to execute fn.

    Args:
        operation : Label string — "provision" | "deploy" | "destroy".
        fn        : Provider method to call (e.g. azure_provider.provision).
        *args     : Positional args forwarded to fn (e.g. config).
        caller_ip : IP of the HTTP caller (used for per-IP concurrency accounting).
        **kwargs  : Keyword args forwarded to fn (NOT including log=).

    Returns:
        The created Job (status=PENDING when returned, RUNNING moments later).

    Example:
        job = launch_job("provision", provider.provision, config, caller_ip=ip)
        # provider.provision(config, log=_log) runs in background thread
    """
    job = job_store.create(operation, caller_ip=caller_ip)
    thread = threading.Thread(
        target=_run_job,
        args=(job, fn, *args),
        kwargs=kwargs,
        daemon=True,      # thread dies when main process exits
        name=f"job-{job.job_id[:8]}",
    )
    thread.start()
    return job
