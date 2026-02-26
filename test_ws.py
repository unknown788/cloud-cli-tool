"""
test_ws.py

Integration test for Phase 5 WebSocket streaming.

Uses FastAPI's TestClient (in-process ASGI transport) so:
  - No separate server process needed.
  - The test shares the same job_store singleton as the app.
  - WebSocket frames are received synchronously via the test client.

Tests two paths:
  A. Late-join  — WS connects after the job already finished (replays history).
  B. Live       — WS connects while job is still producing lines (streams live).

Run:  python test_ws.py
"""

import json
import threading
import time

from fastapi.testclient import TestClient

from api.app import app
from api.jobs import job_store
from api.schemas import JobStatus

client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def collect_ws_frames(job_id: str, timeout: float = 10.0) -> list:
    """Open WS /ws/{job_id} and collect all frames until 'done'."""
    frames = []
    with client.websocket_connect(f"/ws/{job_id}") as ws:
        while True:
            raw = ws.receive_text()
            frame = json.loads(raw)
            frames.append(frame)
            if frame.get("type") == "done":
                break
    return frames


# ---------------------------------------------------------------------------
# Test A — Late-join path
# ---------------------------------------------------------------------------

def test_late_join():
    """
    Create a job that fails instantly (no Azure creds).
    Connect the WS after it has already finished.
    Expect: status=failed, error frame, done frame.
    """
    print("\n── Test A: Late-join ──")

    # Kick off provision (will fail immediately with no creds)
    resp = client.post("/provision", json={
        "provider": "azure",
        "vm_name": "test-vm",
        "location": "eastus",
        "resource_group": "test-rg",
        "admin_username": "azureuser",
        "ssh_key_path": "~/.ssh/id_rsa.pub",
    })
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}"
    job_id = resp.json()["job_id"]
    print(f"  job_id: {job_id}")

    # Wait for background thread to finish
    time.sleep(1.0)

    frames = collect_ws_frames(job_id)
    types = [f["type"] for f in frames]

    print(f"  Frames received: {types}")
    for f in frames:
        print(f"    {f['type']:8s}  {str(f.get('data',''))[:70]}")

    assert "status" in types,  "Missing status frame"
    assert "error"  in types,  "Missing error frame"
    assert "done"   in types,  "Missing done frame"
    assert types[-1] == "done", "done must be last"

    status_frame = next(f for f in frames if f["type"] == "status")
    assert status_frame["data"] == "failed"

    print("  ✅  PASSED")


# ---------------------------------------------------------------------------
# Test B — Live streaming path
# ---------------------------------------------------------------------------

def test_live_streaming():
    """
    Inject a mock job that produces 5 lines over ~1.5 s.
    Connect the WS before it finishes and verify all lines arrive live.
    """
    print("\n── Test B: Live streaming ──")

    # Create a controlled job directly in the job store
    job = job_store.create("deploy")

    def _slow_worker():
        job.status = JobStatus.RUNNING
        for i in range(1, 6):
            line = f"Step {i}/5 — work in progress"
            job.logs.append(line)
            job.log_queue.put(line)
            time.sleep(0.3)
        job.status = JobStatus.SUCCEEDED
        job.log_queue.put(None)   # sentinel

    # Start the slow worker in a daemon thread
    threading.Thread(target=_slow_worker, daemon=True).start()

    # Connect the WS immediately (job is mid-flight)
    t0 = time.time()
    frames = collect_ws_frames(job.job_id, timeout=15.0)
    elapsed = time.time() - t0

    print(f"  Frames received in {elapsed:.1f}s:")
    for f in frames:
        print(f"    {f['type']:8s}  {str(f.get('data',''))[:70]}")

    types = [f["type"] for f in frames]
    log_frames = [f for f in frames if f["type"] == "log"]

    assert len(log_frames) == 5,  f"Expected 5 log frames, got {len(log_frames)}"
    assert elapsed >= 1.0,        "Took <1 s — streaming may not be live"
    assert "status" in types
    assert types[-1] == "done"

    final_status = next(f for f in reversed(frames) if f["type"] == "status")
    assert final_status["data"] == "succeeded", f"Got: {final_status}"

    print(f"  ✅  PASSED  ({len(log_frames)} live log frames, final=succeeded)")


# ---------------------------------------------------------------------------
# Test C — Unknown job_id
# ---------------------------------------------------------------------------

def test_unknown_job():
    """WS for a non-existent job_id should send error then close."""
    print("\n── Test C: Unknown job_id ──")
    frames = []
    with client.websocket_connect("/ws/does-not-exist") as ws:
        raw = ws.receive_text()
        frames.append(json.loads(raw))
        # After error frame, server closes with 4004

    assert frames[0]["type"] == "error"
    print(f"  Error frame: {frames[0]['data']}")
    print("  ✅  PASSED")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_late_join()
    test_live_streaming()
    test_unknown_job()
    print("\n══ All Phase 5 tests passed ✅ ══\n")
