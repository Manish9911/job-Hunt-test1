"""Agent control endpoints: start / stop / status / one-shot apply."""
from __future__ import annotations

import threading
from fastapi import APIRouter, Request

from ..services.agent import get_agent

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/status")
def status(request: Request) -> dict:
    return get_agent(request).status()


@router.post("/start")
def start(request: Request, min_score: int = 0) -> dict:
    agent = get_agent(request)
    started = agent.start(min_score=min_score)
    return {"running": agent.is_running, "started": started,
            "min_score": agent.min_score}


@router.post("/stop")
def stop(request: Request) -> dict:
    agent = get_agent(request)
    agent.stop()
    agent.wait(timeout=10)  # give the worker a moment to exit cleanly
    return {"running": agent.is_running}


@router.post("/apply/{job_id}")
def apply_one(request: Request, job_id: int) -> dict:
    """One-shot: the user clicked Apply on a single job. The agent gates +
    fills + auto-submits that job in a background thread; the page can
    poll /status for the outcome."""
    from ..db.models import Job

    agent = get_agent(request)
    db = request.app.state.SessionLocal()
    try:
        job = db.get(Job, job_id)
    finally:
        db.close()
    if job is None:
        return {"error": "job not found"}

    def _run():
        db2 = request.app.state.SessionLocal()
        try:
            j = db2.get(Job, job_id)
            if j is not None:
                agent._process_job(db2, j)
        except Exception as e:
            agent.last_error = f"one-shot apply failed: {type(e).__name__}: {e}"[:200]
        finally:
            db2.close()

    threading.Thread(target=_run, name=f"agent-one-shot-{job_id}", daemon=True).start()
    return {"queued": job_id, "title": job.title}
