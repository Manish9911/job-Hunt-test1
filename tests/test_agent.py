"""Tests for the auto-apply agent: JD gate + apply loop, start/stop, API."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.db.models import Application, Job, Profile, Resume
from backend.app.services.agent import AutoApplyAgent, MAX_ATTEMPTS


@pytest.fixture()
def app(tmp_path):
    from backend.app.main import create_app
    return create_app(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")


@pytest.fixture()
def agent(app):
    return app.state.agent


@pytest.fixture()
def db(app):
    s = app.state.SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _job(db, n: int, score: int = 50, **kw) -> Job:
    j = Job(
        title=f"SWE {n}",
        company=f"ACME{n}",
        location="Remote, US",
        apply_url=f"http://ats.local/apply/{n}",
        unique_key=f"agent-test-{n}",
        passes_filters=True,
        match_score=score,
        **kw,
    )
    db.add(j)
    db.commit()
    db.refresh(j)
    return j


def _profile_with_resume(db) -> Profile:
    p = Profile(full_name="T", email="t@x.io")
    db.add(p)
    db.commit()
    db.refresh(p)
    r = Resume(profile_id=p.id, label="master",
               original_filename="t.docx", storage_path="data/t.docx",
               is_master=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return p


def _app_row(db, job, status, resume) -> Application:
    a = Application(job_id=job.id, resume_id=resume.id,
                    status=status, dry_run=False)
    db.add(a)
    db.commit()
    return a


def test_next_candidate_orders_by_score(db, agent):
    _job(db, 1, score=40)
    hi = _job(db, 2, score=90)
    _job(db, 3, score=70)
    picked = agent._next_candidate(db)
    assert picked is not None
    assert picked.id == hi.id


def test_next_candidate_skips_handled_jobs(db, agent):
    p = _profile_with_resume(db)
    r = db.query(Resume).filter(Resume.profile_id == p.id).first()
    j = _job(db, 1, score=90)
    _app_row(db, j, "submitted", r)
    assert agent._next_candidate(db) is None


def test_next_candidate_retries_failed_up_to_max(db, agent):
    p = _profile_with_resume(db)
    r = db.query(Resume).filter(Resume.profile_id == p.id).first()
    j = _job(db, 1, score=90)
    assert agent._next_candidate(db) is j
    for _ in range(MAX_ATTEMPTS):
        _app_row(db, j, "failed", r)
    assert agent._next_candidate(db) is None


def test_next_candidate_requeues_job_with_old_handled_row(db, agent):
    """Requeue incident (2026-11-11): a job with an OLD handled row plus a
    newer 'pending' row (false-positive rejection that got requeued) must be
    picked up again — the old 'any handled row => skip' logic made such jobs
    unrunnable while the agent idled on 'no new jobs'."""
    p = _profile_with_resume(db)
    r = db.query(Resume).filter(Resume.profile_id == p.id).first()
    j = _job(db, 1, score=90)
    _app_row(db, j, "rejected_fraud", r)   # old false-positive row
    _app_row(db, j, "pending", r)          # requeued
    assert agent._next_candidate(db) is j


def test_next_candidate_still_skips_job_whose_latest_is_handled(db, agent):
    p = _profile_with_resume(db)
    r = db.query(Resume).filter(Resume.profile_id == p.id).first()
    j = _job(db, 1, score=90)
    _app_row(db, j, "pending", r)
    _app_row(db, j, "submitted", r)        # newest row is handled -> done
    assert agent._next_candidate(db) is None


def test_reject_path_creates_rejected_jd_row(db, monkeypatch, agent):
    j = _job(db, 1, score=90)
    _profile_with_resume(db)

    called = {}

    def _boom(*a, **k):
        called["yes"] = True
        raise AssertionError("runner must not be called on a reject")

    monkeypatch.setattr("backend.app.services.tailor.fetch_jd",
                        lambda job: "US citizens only. No exceptions.")
    monkeypatch.setattr("backend.app.automation.runner.run_application", _boom)

    agent._process_job(db, j)

    assert "yes" not in called
    row = db.query(Application).filter(Application.job_id == j.id).one()
    assert row.status == "rejected_jd"
    assert "US citizens only" in (row.notes_json or "")
    assert agent.stats["rejected"] == 1
    # eligibility persisted on the job
    assess = json.loads(j.jd_eligibility_json) if j.jd_eligibility_json else {}
    assert assess.get("decision") == "reject"


def test_apply_path_submits_and_tallies(db, monkeypatch, agent):
    j = _job(db, 1, score=90)
    _profile_with_resume(db)

    monkeypatch.setattr("backend.app.services.tailor.fetch_jd",
                        lambda job: "We are looking for a senior engineer. No visa talk here.")
    calls = []
    monkeypatch.setattr(
        "backend.app.services.tailor.tailor_job",
        lambda db, job_id, **kw: calls.append(("tailor", job_id)) or {"tailored_resume_id": 1},
    )
    monkeypatch.setattr(
        "backend.app.automation.runner.run_application",
        lambda db, job_id, **kw: calls.append(("run", job_id)) or {"status": "submitted"},
    )

    agent._process_job(db, j)

    assert agent.stats["submitted"] == 1
    # tailor must happen BEFORE the application run (user's vision: tailor -> fill -> submit)
    assert [c[0] for c in calls] == ["tailor", "run"], calls
    assess = json.loads(j.jd_eligibility_json)
    assert assess["decision"] == "apply"
    assert any(e["kind"] == "tailored" for e in agent.events)


def test_walled_board_is_skipped_not_battled(db, agent, monkeypatch):
    """Wall habit: a board with 3+ CAPTCHA strikes is parked as
    skipped_walled and the browser runner is never called."""
    j = _job(db, 1, score=90)
    _profile_with_resume(db)

    from backend.app.services import learn
    for _ in range(3):
        learn.record_outcome(db, j, "captcha_manual")

    monkeypatch.setattr("backend.app.services.tailor.fetch_jd",
                        lambda job: "senior engineer, no visa talk")
    called = []
    monkeypatch.setattr(
        "backend.app.automation.runner.run_application",
        lambda db, job_id, **kw: called.append(job_id) or {"status": "submitted"},
    )

    agent._process_job(db, j)

    assert called == []                      # browser never opened
    db.expire_all()
    rows = db.query(Application).filter(Application.job_id == j.id).all()
    assert rows and rows[0].status == "skipped_walled"


def test_min_score_filters_candidates(db, agent):
    _job(db, 1, score=40)
    hi = _job(db, 2, score=90)
    agent.min_score = 80
    assert agent._next_candidate(db) is hi
    agent.min_score = 95
    assert agent._next_candidate(db) is None
    agent.min_score = 0
    assert agent._next_candidate(db) is hi


def test_agent_start_stop_lifecycle(agent):
    assert not agent.is_running
    assert agent.start() is True
    assert agent.is_running
    assert agent.start() is False          # idempotent
    assert agent.stop() is True
    agent.wait(timeout=30)
    assert not agent.is_running


def test_agent_processes_reject_end_to_end(app, monkeypatch):
    agent = app.state.agent
    db = app.state.SessionLocal()
    try:
        j = _job(db, 1, score=90)
        _profile_with_resume(db)
        monkeypatch.setattr("backend.app.services.tailor.fetch_jd",
                            lambda job: "we are unable to sponsor")
        agent.start()
        deadline = time.time() + 20
        row = None
        while time.time() < deadline:
            db.expire_all()
            row = db.query(Application).filter(Application.job_id == j.id).first()
            if row is not None and row.status == "rejected_jd":
                break
            time.sleep(0.3)
        assert row is not None and row.status == "rejected_jd"
    finally:
        agent.stop()
        agent.wait(timeout=30)
        db.close()


def test_agent_api_endpoints(client, db):
    r = client.get("/api/agent/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert set(body["stats"]) >= {"submitted", "rejected"}

    r = client.post("/api/agent/start")
    assert r.status_code == 200
    assert r.json()["running"] is True

    r = client.post("/api/agent/stop")
    assert r.json()["running"] is False


def test_one_shot_apply_queues(db, client, app, monkeypatch):
    j = _job(db, 1, score=90)
    _profile_with_resume(db)

    done = []
    monkeypatch.setattr(
        "backend.app.services.tailor.fetch_jd", lambda job: "nothing about visas")
    monkeypatch.setattr(
        "backend.app.automation.runner.run_application",
        lambda db, job_id, **kw: (done.append(job_id) or {"status": "submitted"}),
    )

    r = client.post(f"/api/agent/apply/{j.id}")
    assert r.status_code == 200
    assert r.json()["queued"] == j.id

    deadline = time.time() + 15
    while time.time() < deadline and not done:
        time.sleep(0.3)
    assert done == [j.id]
