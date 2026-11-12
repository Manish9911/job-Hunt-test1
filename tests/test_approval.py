"""Tests for the per-submission approval endpoints (permission model,
2026-11-09): POST /api/applications/{id}/submit and /reject.

Only the DB-side paths are exercised here (404/409/reject/row-reuse
preconditions). The approve endpoint's background thread opens a real
browser, which is what the agent/runner integration covers, not unit tests.
"""
from __future__ import annotations

from datetime import datetime

from backend.app.db.models import Application, Job, Profile, Resume


def _make_job(db, apply_url="https://boards.greenhouse.io/acme/jobs/1") -> Job:
    job = Job(
        title="Senior Software Engineer", company="Acme",
        apply_url=apply_url, source="test",
        unique_key=f"test-{datetime.utcnow().timestamp()}-{id(apply_url)}",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _make_profile(db) -> Profile:
    p = Profile(full_name="T", email="t@example.com")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_resume(db) -> Resume:
    p = _make_profile(db)
    r = Resume(profile_id=p.id, original_filename="m.docx", storage_path="x.docx",
               content_json="{}", is_master=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _staged_app(db, job: Job, resume: Resume, status="dry_run_completed") -> Application:
    a = Application(job_id=job.id, resume_id=resume.id, dry_run=True,
                    status=status, created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_approve_unknown_app_404(client):
    r = client.post("/api/applications/999999/submit")
    assert r.status_code == 404


def test_approve_non_staged_409(client, app):
    db = app.state.SessionLocal()
    try:
        job = _make_job(db)
        resume = _make_resume(db)
        a = _staged_app(db, job, resume, status="submitted")
        r = client.post(f"/api/applications/{a.id}/submit")
        assert r.status_code == 409
    finally:
        db.close()


def test_reject_staged_marks_rejected_user(client, app):
    db = app.state.SessionLocal()
    try:
        job = _make_job(db)
        resume = _make_resume(db)
        a = _staged_app(db, job, resume)
        r = client.post(f"/api/applications/{a.id}/reject")
        assert r.status_code == 200
        assert r.json() == {"rejected": True, "application_id": a.id}
        db.expire_all()
        row = db.get(Application, a.id)
        assert row.status == "rejected_user"
        assert row.finished_at is not None
    finally:
        db.close()


def test_reject_non_staged_409(client, app):
    db = app.state.SessionLocal()
    try:
        job = _make_job(db)
        resume = _make_resume(db)
        a = _staged_app(db, job, resume, status="rejected_user")
        r = client.post(f"/api/applications/{a.id}/reject")
        assert r.status_code == 409
    finally:
        db.close()


def test_agent_treats_fraud_and_payment_outcomes_as_handled(client, app):
    """The agent must not retry jobs parked by the permission model."""
    from backend.app.services.agent import _HANDLED_STATUSES

    assert "rejected_fraud" in _HANDLED_STATUSES
    assert "flagged_fraud_review" in _HANDLED_STATUSES
    assert "rejected_user" in _HANDLED_STATUSES
    assert "submit_blocked_payment" in _HANDLED_STATUSES
    assert "submitting" not in _HANDLED_STATUSES
