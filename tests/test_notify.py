"""Tests for the notification system (agent flags CAPTCHA walls to the user)."""
import pytest

from backend.app.db.models import Job, Notification


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


def _job(db, n=1) -> Job:
    j = Job(title=f"SWE {n}", company=f"Co{n}",
            apply_url=f"http://ats.local/{n}",
            unique_key=f"notify-test-{n}", passes_filters=True)
    db.add(j)
    db.commit()
    db.refresh(j)
    return j


def test_notify_persists_row(db, monkeypatch):
    monkeypatch.setattr("backend.app.services.notify._desktop_alert",
                        lambda *a, **k: None)
    from backend.app.services.notify import notify

    note = notify(db, "action", "CAPTCHA needs you", "detail",
                  link="http://ats.local/1")
    assert note.id is not None
    row = db.get(Notification, note.id)
    assert row.kind == "action" and row.title == "CAPTCHA needs you"
    assert row.link == "http://ats.local/1"
    assert row.read_at is None


def _patch_notify(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "backend.app.services.notify.notify",
        lambda db, kind, title, detail="", link=None:
        calls.append((kind, title, link)) or Notification(id=1),
    )
    return calls


def test_agent_captcha_creates_action_notification(db, agent, monkeypatch):
    calls = _patch_notify(monkeypatch)
    j = _job(db, 1)
    agent._notify_result(db, j, "submit_blocked_captcha",
                         {"error": None, "detail_url": "/applications/1"})
    assert calls and calls[0][0] == "action"
    assert "CAPTCHA" in calls[0][1]
    assert calls[0][2] == j.apply_url


def test_agent_captcha_before_fill_also_flags(db, agent, monkeypatch):
    calls = _patch_notify(monkeypatch)
    j = _job(db, 2)
    agent._notify_result(db, j, "captcha_manual",
                         {"error": "CAPTCHA", "detail_url": "/applications/2"})
    assert calls and calls[0][0] == "action"
    assert "CAPTCHA" in calls[0][1]


def test_agent_submitted_creates_info_notification(db, agent, monkeypatch):
    calls = _patch_notify(monkeypatch)
    j = _job(db, 3)
    agent._notify_result(db, j, "submitted",
                         {"error": None, "detail_url": "/applications/9"})
    assert calls and calls[0][0] == "info"
    assert calls[0][2] == "/applications/9"


def test_agent_failure_creates_action_notification(db, agent, monkeypatch):
    calls = _patch_notify(monkeypatch)
    j = _job(db, 4)
    agent._notify_result(db, j, "failed",
                         {"error": "boom", "detail_url": None})
    assert calls and calls[0][0] == "action"


def test_api_badge_panel_and_read(app, db, client, monkeypatch):
    monkeypatch.setattr("backend.app.services.notify._desktop_alert",
                        lambda *a, **k: None)
    from backend.app.services.notify import notify

    assert client.get("/api/notifications/badge").text == ""
    notify(db, "action", "CAPTCHA needs you: Co1", "open the link",
           link="http://ats.local/1")
    db.expire_all()

    badge = client.get("/api/notifications/badge").text
    assert ">1<" in badge

    panel = client.get("/api/notifications/panel").text
    assert "CAPTCHA needs you: Co1" in panel
    assert "http://ats.local/1" in panel

    r = client.post("/api/notifications/read-all")
    assert r.json()["read"] == 1
    db.expire_all()
    assert client.get("/api/notifications/badge").text == ""


def test_api_read_one(app, db, client, monkeypatch):
    monkeypatch.setattr("backend.app.services.notify._desktop_alert",
                        lambda *a, **k: None)
    from backend.app.services.notify import notify

    n1 = notify(db, "info", "one")
    n2 = notify(db, "action", "two")
    db.expire_all()

    assert client.post(f"/api/notifications/{n1.id}/read").json()["read"] == n1.id
    badge = client.get("/api/notifications/badge").text
    assert ">1<" in badge  # only n2 still unread
    assert client.post("/api/notifications/99999/read").json() == {"error": "not found"}
