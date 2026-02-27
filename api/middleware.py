"""
api/middleware.py

Abuse-protection layer for the CloudLaunch public API.
Designed for a *public portfolio demo* where real Azure money is at stake.

Guards (applied in order by FastAPI Depends chain):
═══════════════════════════════════════════════════

1.  IP Rate Limiter  (Starlette middleware, hits every request)
    ─────────────────────────────────────────────────────────────
    Token-bucket per IP.  Write ops throttle much faster than reads.

    Env vars:
        RATE_LIMIT_READ_RPM   default 60   — reads per IP per minute
        RATE_LIMIT_WRITE_RPM  default 4    — writes per IP per minute

2.  API-Key Authentication  (FastAPI Depends)
    ─────────────────────────────────────────
    All mutating endpoints require X-API-Key header.
    Key is set via API_KEY env var.  If unset → 503 (API closed).

    Optional key-use cap:
        API_KEY_MAX_USES  default 0 (unlimited)
    When > 0, the API key becomes invalid after that many successful
    mutations — a "limited-use token" that you share per visitor.

3.  Global Concurrency + Budget Cap  (FastAPI Depends)
    ─────────────────────────────────────────────────────
    a) MAX_CONCURRENT_JOBS  default 1
       At most N jobs running simultaneously, globally.

    b) MAX_TOTAL_PROVISIONS  default 3
       Hard lifetime cap on how many /provision calls are ever accepted.
       *** THIS IS YOUR PRIMARY WALLET GUARD ***
       Once hit, the endpoint returns 403 until you restart.
       Set to 0 to disable.

    c) MAX_JOBS_PER_IP  default 1
       Per-IP running job cap.

4.  Auto-Destroy TTL  (background timer, started on provision success)
    ─────────────────────────────────────────────────────────────────
    A timer fires AUTO_DESTROY_MINUTES (default 30) after a successful
    provision and automatically tears down all cloud resources.
    This means even if a visitor walks away, the VM self-destructs.
    Set AUTO_DESTROY_MINUTES=0 to disable.

5.  Ops Audit Log
    ─────────────────────────────────────────────────
    Every mutating call appends to audit.log:
        2026-02-27T12:34:56Z  PROVISION  1.2.3.4
"""

from __future__ import annotations

import os
import time
import threading
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# ─────────────────────────────────────────────────────────────────────────────
# Configuration — every knob is an env var so you tune without code changes
# ─────────────────────────────────────────────────────────────────────────────

_RATE_READ_RPM:     int           = int(os.getenv("RATE_LIMIT_READ_RPM",  "60"))
_RATE_WRITE_RPM:    int           = int(os.getenv("RATE_LIMIT_WRITE_RPM", "4"))
_MAX_CONCURRENT:    int           = int(os.getenv("MAX_CONCURRENT_JOBS",   "1"))
_MAX_PER_IP:        int           = int(os.getenv("MAX_JOBS_PER_IP",       "1"))
_MAX_PROVISIONS:    int           = int(os.getenv("MAX_TOTAL_PROVISIONS",  "3"))
_AUTO_DESTROY_MINS: int           = int(os.getenv("AUTO_DESTROY_MINUTES",  "30"))
_KEY_MAX_USES:      int           = int(os.getenv("API_KEY_MAX_USES",      "0"))  # 0 = unlimited
_API_KEY:           Optional[str] = os.getenv("API_KEY")

_WRITE_PATHS = {"/provision", "/deploy", "/destroy"}

# ─────────────────────────────────────────────────────────────────────────────
# Audit logger — writes to audit.log in cwd
# ─────────────────────────────────────────────────────────────────────────────

_audit_logger = logging.getLogger("cloudlaunch.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
_audit_handler = logging.FileHandler("audit.log", encoding="utf-8")
_audit_handler.setFormatter(logging.Formatter("%(message)s"))
_audit_logger.addHandler(_audit_handler)


def write_audit(operation: str, ip: str, extra: str = "") -> None:
    """Append one line to audit.log: timestamp  OPERATION  ip  [extra]."""
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [ts, operation.upper(), ip]
    if extra:
        parts.append(extra)
    _audit_logger.info("  ".join(parts))


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Per-IP token-bucket rate limiter  (Starlette middleware)
# ─────────────────────────────────────────────────────────────────────────────

class _Bucket:
    """Token bucket for one IP × one operation class."""
    __slots__ = ("tokens", "last_refill", "capacity", "refill_rate")

    def __init__(self, capacity: float, refill_rate: float) -> None:
        self.capacity     = capacity
        self.refill_rate  = refill_rate       # tokens per second
        self.tokens       = capacity          # start full
        self.last_refill  = time.monotonic()

    def consume(self, cost: float = 1.0) -> bool:
        now              = time.monotonic()
        elapsed          = now - self.last_refill
        self.tokens      = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP token-bucket rate limiter applied to every request."""

    def __init__(self, app, **kwargs) -> None:
        super().__init__(app, **kwargs)
        self._lock          = threading.Lock()
        self._read_buckets  : dict = defaultdict(
            lambda: _Bucket(_RATE_READ_RPM,  _RATE_READ_RPM  / 60))
        self._write_buckets : dict = defaultdict(
            lambda: _Bucket(_RATE_WRITE_RPM, _RATE_WRITE_RPM / 60))

    @staticmethod
    def _ip(request: Request) -> str:
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next: Callable):
        path     = request.url.path
        ip       = self._ip(request)
        is_write = any(path.startswith(p) for p in _WRITE_PATHS)

        with self._lock:
            allowed = (
                self._write_buckets[ip].consume()
                if is_write
                else self._read_buckets[ip].consume()
            )

        if not allowed:
            rpm  = _RATE_WRITE_RPM if is_write else _RATE_READ_RPM
            kind = "write" if is_write else "read"
            write_audit("RATE_LIMITED", ip, path)
            return JSONResponse(
                status_code=429,
                content={"detail": (
                    f"Rate limit exceeded ({rpm} {kind} req/min per IP). "
                    "Please wait before retrying."
                )},
                headers={"Retry-After": "60"},
            )

        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# 2.  API-key authentication  (FastAPI Depends)
# ─────────────────────────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

_key_use_lock  = threading.Lock()
_key_use_count : int = 0


def require_api_key(
    request: Request,
    key: Optional[str] = Depends(_api_key_header),
) -> None:
    """
    FastAPI dependency for all mutating endpoints.

    - API_KEY not set              → 503  (operator must configure first)
    - Header missing / wrong       → 401
    - API_KEY_MAX_USES exceeded    → 403  (key burned out)
    - Correct key within budget    → passes, increments use counter
    """
    global _key_use_count

    if _API_KEY is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "This API is not open to the public yet. "
                "The server operator must set the API_KEY environment variable."
            ),
        )

    if key != _API_KEY:
        ip = _get_client_ip(request)
        write_audit("AUTH_FAIL", ip, request.url.path)
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Set it in the X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if _KEY_MAX_USES > 0:
        with _key_use_lock:
            if _key_use_count >= _KEY_MAX_USES:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"This API key has reached its use limit "
                        f"({_KEY_MAX_USES} operations). Contact the site owner."
                    ),
                )
            _key_use_count += 1


def get_key_usage() -> dict:
    """Return current key usage (exposed via GET /quota)."""
    with _key_use_lock:
        used = _key_use_count
    return {
        "key_uses_used":  used,
        "key_uses_limit": _KEY_MAX_USES if _KEY_MAX_USES > 0 else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3a.  Global concurrency + per-IP cap  (FastAPI Depends)
# ─────────────────────────────────────────────────────────────────────────────

_concurrency_lock = threading.Lock()
_running_jobs : dict = defaultdict(int)   # ip → count
_global_running : int = 0


def check_concurrency(request: Request) -> None:
    """
    FastAPI dependency — reserves a job slot before launching background work.
    The slot is released by release_concurrency_slot() when the thread ends.
    """
    global _global_running
    ip = _get_client_ip(request)

    with _concurrency_lock:
        if _global_running >= _MAX_CONCURRENT:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Server is busy — {_MAX_CONCURRENT} job(s) already running. "
                    "Wait for the current operation to finish."
                ),
                headers={"Retry-After": "30"},
            )
        if _running_jobs[ip] >= _MAX_PER_IP:
            raise HTTPException(
                status_code=429,
                detail="You already have a running job. Wait for it to complete.",
                headers={"Retry-After": "30"},
            )
        _global_running   += 1
        _running_jobs[ip] += 1


def release_concurrency_slot(ip: str) -> None:
    """Called by api/jobs._run_job when a job finishes (success or failure)."""
    global _global_running
    with _concurrency_lock:
        _global_running = max(0, _global_running - 1)
        if _running_jobs[ip] > 0:
            _running_jobs[ip] -= 1


# ─────────────────────────────────────────────────────────────────────────────
# 3b.  Provision budget cap  (FastAPI Depends — provision endpoint only)
#      *** PRIMARY WALLET GUARD ***
# ─────────────────────────────────────────────────────────────────────────────

_provision_lock    = threading.Lock()
_provision_count   : int = 0   # number currently provisioned (active VMs)
_provision_total   : int = 0   # lifetime total provisions made (never decrements)


def check_provision_budget(request: Request) -> None:
    """
    FastAPI dependency — only on POST /provision.

    Tracks *active* VMs (provisioned minus destroyed).  This means once you
    destroy a VM the slot is freed and you can provision again — up to
    MAX_TOTAL_PROVISIONS active VMs at any one time.

    Set MAX_TOTAL_PROVISIONS=0 in env to disable the cap entirely.
    """
    global _provision_count

    if _MAX_PROVISIONS == 0:
        return   # cap disabled

    ip = _get_client_ip(request)
    with _provision_lock:
        if _provision_count >= _MAX_PROVISIONS:
            write_audit("BUDGET_EXCEEDED", ip)
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Provision budget full — {_provision_count}/{_MAX_PROVISIONS} "
                    f"active VM(s) already running. "
                    "Destroy an existing VM first, then provision a new one."
                ),
            )
        _provision_count += 1
        global _provision_total
        _provision_total += 1


def release_provision_slot() -> None:
    """
    Decrement the active-VM counter.
    Called automatically after every successful destroy (manual or auto-destroy).
    This frees the slot so the user can provision a new VM.
    """
    global _provision_count
    with _provision_lock:
        _provision_count = max(0, _provision_count - 1)


def reset_provision_counter() -> int:
    """
    Emergency reset: set the active-VM counter to 0.
    Returns the old value.
    Use when VMs were manually deleted via Azure Portal/CLI and the
    in-memory counter is stuck.  Does NOT touch any Azure resources.
    """
    global _provision_count
    with _provision_lock:
        old = _provision_count
        _provision_count = 0
    return old


def get_provision_quota() -> dict:
    """Return current provision usage (exposed via GET /quota)."""
    with _provision_lock:
        active  = _provision_count
        total   = _provision_total
    return {
        "provisions_active":  active,
        "provisions_limit":   _MAX_PROVISIONS if _MAX_PROVISIONS > 0 else None,
        "provisions_total":   total,
        "auto_destroy_minutes": _AUTO_DESTROY_MINS if _AUTO_DESTROY_MINS > 0 else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Auto-destroy TTL
# ─────────────────────────────────────────────────────────────────────────────

_auto_destroy_timer : Optional[threading.Timer] = None


def schedule_auto_destroy(
    state: dict,
    provider_name: str,
    state_file: str,
    log_fn: Callable[[str], None] = print,
) -> Optional[threading.Timer]:
    """
    Schedule automatic destroy AUTO_DESTROY_MINUTES minutes from now.
    Returns the Timer so callers can cancel it on manual destroy.
    Returns None if AUTO_DESTROY_MINUTES == 0 (feature disabled).
    """
    global _auto_destroy_timer

    if _AUTO_DESTROY_MINS <= 0:
        return None

    def _do_destroy() -> None:
        from providers import get_provider   # lazy import — avoids circular dep
        import os as _os

        log_fn(f"⏰ Auto-destroy triggered (TTL={_AUTO_DESTROY_MINS} min). Tearing down VM…")
        write_audit("AUTO_DESTROY", "system", f"ttl={_AUTO_DESTROY_MINS}min")
        try:
            p = get_provider(provider_name)
            p.destroy(state, log=log_fn)
            if _os.path.exists(state_file):
                _os.remove(state_file)
            release_provision_slot()   # free the budget slot so new VMs can be provisioned
            log_fn("✅ Auto-destroy complete — all resources deleted. Provision slot freed.")
        except Exception as exc:
            log_fn(f"❌ Auto-destroy failed: {exc}")

    timer = threading.Timer(_AUTO_DESTROY_MINS * 60, _do_destroy)
    timer.daemon = True
    timer.name   = "auto-destroy"
    timer.start()
    _auto_destroy_timer = timer
    log_fn(
        f"⚠️  This VM will be automatically destroyed in {_AUTO_DESTROY_MINS} min. "
        "Click Destroy manually before then to cancel the timer."
    )
    return timer


def cancel_auto_destroy() -> bool:
    """Cancel the pending auto-destroy timer. Returns True if one was active."""
    global _auto_destroy_timer
    if _auto_destroy_timer and _auto_destroy_timer.is_alive():
        _auto_destroy_timer.cancel()
        _auto_destroy_timer = None
        write_audit("AUTO_DESTROY_CANCELLED", "manual")
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
