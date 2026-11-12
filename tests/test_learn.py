"""Tests for the self-improvement loop (habit inherited from the
job-apply-agent skill, 2026-11-09): lessons the agent learns from outcomes
and the user's decisions, and the handoff file.
"""
from __future__ import annotations

from datetime import datetime

from backend.app.db.models import Application, Job, Lesson, Profile, Resume
from backend.app.services import learn
from backend.app.services.fraud import _domain


def _job(db, company="Acme", url="https://boards.greenhouse.io/acme/jobs/1"):
    job = Job(
        title="Senior SWE", company=company, apply_url=url, source="test",
        unique_key=f"t-{datetime.utcnow().timestamp()}-{id(url)}",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _resume(db):
    p = Profile(full_name="T", email="t@e.com")
    db.add(p)
    db.commit()
    r = Resume(profile_id=p.id, original_filename="m.docx",
               storage_path="x.docx", content_json="{}", is_master=True)
    db.add(r)
    db.commit()
    return r


# ---------------------------------------------------------------- wall habit
def test_three_captcha_walls_create_wall_lesson(client, app):
    db = app.state.SessionLocal()
    try:
        job = _job(db)
        for _ in range(3):
            learn.record_outcome(db, job, "captcha_manual")
        wall = learn.board_is_walled(db, job)
        assert wall is not None
        assert wall.kind == "wall"
        assert wall.count >= 3
    finally:
        db.close()


def test_two_walls_do_not_wall_yet(client, app):
    db = app.state.SessionLocal()
    try:
        job = _job(db, url="https://jobs.lever.co/acme/2")
        learn.record_outcome(db, job, "captcha_manual")
        learn.record_outcome(db, job, "captcha_manual")
        assert learn.board_is_walled(db, job) is None
    finally:
        db.close()


def test_success_clears_the_wall(client, app):
    db = app.state.SessionLocal()
    try:
        job = _job(db)
        for _ in range(3):
            learn.record_outcome(db, job, "captcha_manual")
        assert learn.board_is_walled(db, job) is not None
        learn.record_outcome(db, job, "submitted")
        assert learn.board_is_walled(db, job) is None
    finally:
        db.close()


def test_user_clears_wall(client, app):
    db = app.state.SessionLocal()
    try:
        job = _job(db)
        for _ in range(3):
            learn.record_outcome(db, job, "captcha_manual")
        learn.user_clears_wall(db, job.apply_url, job.company)
        assert learn.board_is_walled(db, job) is None
    finally:
        db.close()


# ------------------------------------------------------------- fraud lessons
def test_scam_verdict_never_reasked(client, app):
    db = app.state.SessionLocal()
    try:
        job = _job(db, company="FishyCorp",
                   url="https://careers-apply.xyz/form")
        learn.user_marks_domain(db, job.apply_url, job.company, "scam")
        assert learn.domain_verdict(db, job) == "scam"
    finally:
        db.close()


def test_legit_verdict_downgrades_review(client, app):
    db = app.state.SessionLocal()
    try:
        job = _job(db, company="FishyCorp",
                   url="https://careers-apply.xyz/form")
        learn.user_marks_domain(db, job.apply_url, job.company, "ok")
        assert learn.domain_verdict(db, job) == "ok"
    finally:
        db.close()


def test_payment_block_does_not_auto_poison_domain(client, app):
    """Method/Ashby incident (2026-11-11): one payment-block (or false
    positive) on an ATS domain must NOT hard-reject every other job there.
    Only the user's explicit 'Mark as scam' click writes that lesson."""
    db = app.state.SessionLocal()
    try:
        job = _job(db)
        learn.record_outcome(db, job, "submit_blocked_payment")
        assert learn.domain_verdict(db, job) is None
        # the user's explicit decision still works
        learn.user_marks_domain(db, job.apply_url, job.company, "scam")
        assert learn.domain_verdict(db, job) == "scam"
    finally:
        db.close()


# ------------------------------------------------------------- lessons API
def test_lessons_crud(client):
    r = client.post("/api/lessons", json={
        "scope": "company:acme", "kind": "tip", "text": "skip intern postings"})
    assert r.status_code == 200
    lid = r.json()["id"]
    rows = client.get("/api/lessons").json()
    assert any(l["id"] == lid for l in rows)
    assert client.delete(f"/api/lessons/{lid}").status_code == 200
    assert not any(l["id"] == lid for l in client.get("/api/lessons").json())


def test_lessons_rejects_bad_scope_and_kind(client):
    assert client.post("/api/lessons",
                       json={"scope": "bogus:x", "kind": "tip",
                             "text": "hi"}).status_code == 400
    assert client.post("/api/lessons",
                       json={"scope": "global", "kind": "vibes",
                             "text": "hi"}).status_code == 400


def test_overview_page_lists_lessons_section(client):
    client.post("/api/lessons",
                json={"scope": "global", "kind": "pref",
                      "text": "never apply to fintech"})
    html = client.get("/").text
    assert "Lessons learned" in html
    assert "never apply to fintech" in html


# -------------------------------------------------------------- handoff file
def test_handoff_file_roundtrip(tmp_path):
    learn.write_status_file(tmp_path, {"running_at_end": True, "stats": {}})
    data = learn.read_status_file(tmp_path)
    assert data["running_at_end"] is True
    learn.write_status_file(tmp_path, {"running_at_end": False})
    assert learn.read_status_file(tmp_path)["running_at_end"] is False


def test_domain_helper():
    assert _domain("https://boards.greenhouse.io/x") == "greenhouse.io"
    assert _domain("http://careers.acme.com/a/b") == "acme.com"
    assert _domain(None) == ""
