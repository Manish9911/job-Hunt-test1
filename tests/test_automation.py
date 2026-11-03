"""Phase 4 — application automation tests.

Unit tests (no browser): adapter detection, context building, captcha
patterns, DB relationships, API shape.

E2E tests (real headless Chromium against a LOCAL mock ATS server):
  * Greenhouse-style form  -> fields filled + file uploaded + consents ticked
  * Lever-style form       -> filled
  * CAPTCHA page           -> aborted as captcha_manual, nothing filled
  * unknown ATS URL        -> manual_required
Skipped entirely when the Chromium binary is not installed.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.automation.adapters import detect_adapter
from backend.app.automation.base import ApplicationContext, CaptchaDetected, check_captcha
from backend.app.automation.runner import run_application
from backend.app.db.models import (
    ActionLog,
    Application,
    CommonAnswer,
    Job,
    Profile,
    Resume,
)

# ---------------------------------------------------------------------------
# Chromium availability (E2E skipped if the browser isn't installed)
# ---------------------------------------------------------------------------


def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return True
    except Exception:
        return False


HAS_CHROMIUM = _chromium_ok()
requires_chromium = pytest.mark.skipif(not HAS_CHROMIUM, reason="Chromium browser not installed")

PROFILE_KW = dict(
    full_name="Manish Badugu",
    email="badugu.m7.test@example.com",
    phone="+1-555-0100",
    location="Hyderabad, India (open to US remote)",
    work_authorization="US — H-1B sponsorship needed",
    sponsorship_needed=True,
    locations_json='["US (remote + onsite)", "EU (remote)"]',
)


def _make_docx(path) -> None:
    from docx import Document

    d = Document()
    d.add_paragraph("Manish Badugu — test resume")
    d.save(str(path))


def _seed(app, apply_url: str, title: str = "Software Engineer (Java)",
          company: str = "MockCorp", source: str = "unknown") -> tuple[int, int]:
    """Create profile + master resume + job. Returns (profile_id, job_id)."""
    Session = app.state.SessionLocal
    db = Session()
    try:
        profile = Profile(**PROFILE_KW)
        db.add(profile)
        db.commit()
        db.refresh(profile)
        for q, a in (
            ("Why are you interested in this role?", "Java/Spring stack matches my 4+ years of experience."),
            ("Are you authorized to work in the US?", "Yes, with H-1B sponsorship."),
            ("What is your notice period / availability to start?", "Notice period of 1 month."),
        ):
            db.add(CommonAnswer(profile_id=profile.id, question=q, answer=a))
        docx_path = app.state.data_dir / f"test_resume_{profile.id}.docx"
        _make_docx(docx_path)
        uniq = f"test-{profile.id}"  # profile id differs per seed → unique_key unique
        resume = Resume(
            profile_id=profile.id,
            label="master",
            original_filename=f"resume_{profile.id}.docx",
            storage_path=str(docx_path),
            content_json="{}",
            is_master=True,
        )
        db.add(resume)
        db.commit()
        db.refresh(resume)
        job = Job(
            title=title,
            company=company,
            apply_url=apply_url,
            source=source,
            hiringcafe_id=uniq,
            passes_filters=True,
            filter_reasons_json="[]",
            unique_key=uniq,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return profile.id, job.id
    finally:
        db.close()


def _run(app, job_id: int, **kw) -> dict:
    import time

    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=True, **kw)
    finally:
        db.close()
    time.sleep(0.8)  # backstop: let the mock's async capture POSTs land
    return result


def _captures(form: str) -> dict[str, str]:
    with LOCK:
        rows = list(CAPTURES.get(form, []))
    return {c["field"]: c["value"] for c in rows}


def _wait_capture(form: str, field: str, timeout: float = 8.0) -> str | None:
    """Poll until the mock server has captured a change on `field`."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        caps = _captures(form)
        if field in caps:
            return caps[field]
        time.sleep(0.25)
    return _captures(form).get(field)


# ---------------------------------------------------------------------------
# Unit tests (no browser)
# ---------------------------------------------------------------------------


def test_detect_adapter_urls():
    assert detect_adapter("https://job-boards.greenhouse.io/acme/jobs/123").name == "greenhouse"
    assert detect_adapter("https://boards.greenhouse.io/acme").name == "greenhouse"
    assert detect_adapter("https://jobs.lever.co/acme/abc").name == "lever"
    assert detect_adapter("https://apply.lever.co/x").name == "lever"
    assert detect_adapter("https://jobs.ashbyhq.com/acme/x").name == "ashby"
    assert detect_adapter("https://acme.wd1.myworkdayjobs.com/external").name == "workday"
    assert detect_adapter("https://acme.com/careers/apply") is None
    assert detect_adapter("") is None


def test_context_from_profile():
    from backend.app.db.models import Profile as P

    p = P(full_name="Manish Badugu", email="e@example.com", phone="+1-555",
          location="Hyderabad", work_authorization="US — H-1B",
          sponsorship_needed=True,
          locations_json='["EU (remote)"]')
    ctx = ApplicationContext.from_profile(p)
    assert ctx.first_name == "Manish"
    assert ctx.last_name == "Badugu"
    assert ctx.email == "e@example.com"
    assert ctx.region == "eu"
    assert ctx.sponsorship_needed is True

    p2 = P(full_name="Only", email="x@y.z",
           locations_json='["US (remote + onsite)"]')
    ctx2 = ApplicationContext.from_profile(p2)
    assert ctx2.first_name == "Only"
    assert ctx2.last_name == ""
    assert ctx2.region == "us"


def test_check_captcha_patterns():
    class FakePage:
        def __init__(self, html):
            self._html = html

        def content(self):
            return self._html

    clean = "<html><form><input name='email'></form></html>"
    check_captcha(FakePage(clean))  # no raise

    for html, label in (
        ('<div class="g-recaptcha" data-sitekey="abc"></div>', "reCAPTCHA"),
        ('<script src="https://hcaptcha.com/1/api.js"></script>', "hCaptcha"),
        ('<div class="cf-turnstile"></div>', "Turnstile"),
        ("<h1>Just a moment...</h1>", "Cloudflare"),
        ("<p>Please verify you are human</p>", "human-verification"),
    ):
        with pytest.raises(CaptchaDetected) as ei:
            check_captcha(FakePage(html))
        assert label in str(ei.value)


def test_match_answer_thresholds():
    from backend.app.automation.adapters import _match_answer

    answers = [
        ("What is your notice period / availability to start?", "Notice period of 1 month."),
        ("Why are you interested in this role?", "Java/Spring stack matches my experience."),
    ]
    assert _match_answer(
        "Why are you interested in this role?", answers
    ) == "Java/Spring stack matches my experience."
    assert _match_answer(
        "Please tell us your notice period / availability to start, if any.", answers
    ) == "Notice period of 1 month."
    # below threshold -> no guess
    assert _match_answer("What is your favorite color?", answers) is None
    assert _match_answer("x y z", answers) is None  # too short to match anything


def test_application_and_actionlog_models(app):
    Session = app.state.SessionLocal
    db = Session()
    try:
        profile_id, job_id = _seed(app, "https://example.com/greenhouse.io/x")
        resume = db.query(Resume).filter_by(profile_id=profile_id).first()
        a = Application(job_id=job_id, resume_id=resume.id, status="pending")
        db.add(a)
        db.commit()
        db.refresh(a)
        db.add(ActionLog(application_id=a.id, job_id=job_id,
                         action="filled", field="email", detail="e@example.com"))
        db.commit()
        a2 = db.get(Application, a.id)
        assert len(a2.action_log) == 1
        assert a2.action_log[0].field == "email"
        assert a2.job.title.startswith("Software")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Mock ATS server (local, in-process)
# ---------------------------------------------------------------------------

CAPTURES: dict[str, list[dict]] = {}
LOCK = threading.Lock()


class MockHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = FORMS.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/submit":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)
            body = SUBMIT_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/capture":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) or b"{}"
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        with LOCK:
            CAPTURES.setdefault(data.get("form", "?"), []).append(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


_CAPTURE_JS = """
<script>
  document.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA')) return;
    var value;
    if (el.type === 'file') value = el.files && el.files[0] ? el.files[0].name : 'none';
    else if (el.type === 'checkbox' || el.type === 'radio') value = String(el.checked);
    else value = el.value;
    fetch('/capture', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({form: 'FORM', field: el.name || el.id || '(anon)', value: value})})
      .catch(function () {});
  }, true);
</script>
"""

GREENHOUSE_HTML = """
<html><body><form method="post" action="/submit">
  <label>First name <input name="fields[first_name]"></label>
  <label>Last name <input name="fields[last_name]"></label>
  <label>Email <input name="fields[email]"></label>
  <label>Phone <input name="fields[phone]"></label>
  <label>Resume <input type="file" name="resume"></label>
  <p>Why are you interested in this role?</p>
  <textarea name="motivation"></textarea>
  <p>What is your notice period / availability to start?</p>
  <textarea name="notice"></textarea>
  <p>What is your favorite color?</p>
  <textarea name="color"></textarea>
  <label><input type="checkbox" name="gdpr_consent"> I consent to the processing of my personal data (GDPR / data protection)</label>
  <label><input type="radio" name="wa" value="no"> I do not require visa sponsorship (no sponsorship needed)</label>
  <label><input type="radio" name="wa" value="h1b"> I require H-1B visa sponsorship</label>
  <button type="submit">Apply</button>
</form></body></html>
"""

LEVER_HTML = """
<html><body><form method="post" action="/submit">
  <input id="firstName" name="firstName" placeholder="First name">
  <input id="lastName" name="lastName" placeholder="Last name">
  <input id="email" name="email" placeholder="Email">
  <input id="phone" name="phone" placeholder="Phone">
  <input type="file" name="resume">
  <button type="submit">Submit application</button>
</form></body></html>
"""

CAPTCHA_HTML = """
<html><body><h1>Apply to MockCorp</h1>
<div class="g-recaptcha" data-sitekey="TESTKEY123"></div>
<form><input name="email"></form>
</body></html>
"""

UNKNOWN_HTML = """
<html><body><h1>MockCorp careers</h1>
<p>Please contact us to apply.</p></body></html>
"""

SUBMIT_HTML = """
<html><body><h1>Thank you!</h1>
<p>Your application has been received. Next steps will follow by email.</p>
</body></html>
"""

def _form_html(body_html: str, form_name: str) -> str:
    return body_html + _CAPTURE_JS.replace("FORM", form_name)


FORMS = {
    "/greenhouse/1": _form_html(GREENHOUSE_HTML, "greenhouse"),
    "/lever/1": _form_html(LEVER_HTML, "lever"),
    "/captcha": CAPTCHA_HTML,
    "/unknown": UNKNOWN_HTML,
}


@pytest.fixture(scope="module")
def mock_ats():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    with LOCK:
        CAPTURES.clear()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------------
# E2E: real Chromium vs local mock ATS
# ---------------------------------------------------------------------------


@requires_chromium
def test_e2e_greenhouse_dry_run(app, mock_ats):
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1", source="greenhouse")
    result = _run(app, job_id)

    assert result["status"] == "dry_run_completed", result
    assert result["adapter"] == "greenhouse"
    filled = result["filled_fields"]
    for f in ("first_name", "last_name", "email", "phone"):
        assert f in filled, f"{f} not in {filled}"

    # screenshot artifact exists
    assert result["screenshot_path"]
    assert os.path.exists(result["screenshot_path"])

    # action log written
    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        log = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in log}
        assert "filled" in actions
        assert "uploaded_file" in actions
        assert "consent_checked" in actions
        email_logs = [l for l in log if l.field == "email"]
        assert email_logs and email_logs[0].detail == PROFILE_KW["email"]
    finally:
        db.close()

    # the mock server actually received the typed values
    assert _wait_capture("greenhouse", "fields[email]") == PROFILE_KW["email"]
    assert _wait_capture("greenhouse", "fields[first_name]") == "Manish"
    assert _wait_capture("greenhouse", "fields[last_name]") == "Badugu"
    assert _wait_capture("greenhouse", "gdpr_consent") == "true"
    assert _wait_capture("greenhouse", "wa") == "true"  # H-1B radio checked
    assert (_wait_capture("greenhouse", "resume") or "").endswith(".docx")

    # screening questions: stored answers auto-filled, unknown left blank
    assert _wait_capture("greenhouse", "motivation") == \
        "Java/Spring stack matches my 4+ years of experience."
    assert _wait_capture("greenhouse", "notice") == "Notice period of 1 month."
    assert _wait_capture("greenhouse", "color") is None  # never guessed


class _FakeLLM:
    """Stand-in LLM: is_available()=True, returns canned replies."""
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def is_available(self):
        return True

    def complete(self, system, user, temperature=0.2, timeout=90.0):
        self.calls += 1
        if len(self.replies) > 1:
            return self.replies.pop(0)
        return self.replies[0]


@requires_chromium
def test_e2e_ai_draft_for_random_question(app, mock_ats, monkeypatch):
    """User rule: random screening questions get an AI-drafted, truth-checked
    answer (logged as ai_draft_answer) instead of being left blank."""
    import backend.app.llm as llm_mod

    ai_answer = "Blue, honestly — I pick tech stacks, not paint colors."
    fake = _FakeLLM([ai_answer])
    monkeypatch.setattr(llm_mod, "get_llm", lambda settings: fake)

    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1", source="greenhouse")
    result = _run(app, job_id)
    assert result["status"] == "dry_run_completed", result

    # the previously-blank "favorite color" field is now filled by the drafter
    assert _wait_capture("greenhouse", "color") == ai_answer
    # stored answers still win over the AI (no LLM call for them)
    assert _wait_capture("greenhouse", "notice") == "Notice period of 1 month."
    assert _wait_capture("greenhouse", "motivation") == \
        "Java/Spring stack matches my 4+ years of experience."
    assert fake.calls == 1  # only the unknown question reached the LLM

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        log = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in log}
        assert "ai_draft_answer" in actions
        assert "ai_drafter_ready" in actions
    finally:
        db.close()


@requires_chromium
def test_e2e_lever_dry_run(app, mock_ats):
    profile_id, job_id = _seed(app, f"{mock_ats}/lever/1", title="SWE II", company="LeverCo", source="lever")
    result = _run(app, job_id)
    assert result["status"] == "dry_run_completed", result
    assert result["adapter"] == "lever"
    assert "email" in result["filled_fields"]
    assert _wait_capture("lever", "email") == PROFILE_KW["email"]


@requires_chromium
def test_e2e_captcha_aborts(app, mock_ats):
    profile_id, job_id = _seed(app, f"{mock_ats}/captcha", title="SWE (captcha site)", source="greenhouse")
    result = _run(app, job_id)
    assert result["status"] == "captcha_manual", result
    assert "CAPTCHA" in (result.get("error") or "")
    assert result["filled_fields"] == []
    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        actions = {l.action for l in db.query(ActionLog).filter_by(application_id=app_row.id)}
        assert "captcha_detected" in actions
        assert "filled" not in actions
    finally:
        db.close()


@requires_chromium
def test_e2e_unknown_ats_manual(app, mock_ats):
    profile_id, job_id = _seed(app, f"{mock_ats}/unknown", title="SWE (plain site)", source="hiringcafe")
    result = _run(app, job_id)
    assert result["status"] == "manual_required", result
    assert result["adapter"] is None


@requires_chromium
def test_e2e_api_endpoints(app, mock_ats, client):
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1", title="SWE (api)", company="APICorp", source="greenhouse")
    r = client.post(f"/api/jobs/{job_id}/apply", json={"dry_run": True})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "dry_run_completed"
    app_id = data["application_id"]

    listing = client.get("/api/applications").json()
    assert any(a["id"] == app_id for a in listing)

    detail = client.get(f"/api/applications/{app_id}").json()
    assert detail["status"] == "dry_run_completed"
    assert any(e["action"] == "filled" for e in detail["action_log"])

    shot = client.get(f"/api/applications/{app_id}/screenshot")
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/png"
    assert shot.content[:4] == b"\x89PNG"

    assert client.get("/api/applications/999999").status_code == 404
    assert client.get("/api/applications/999999/screenshot").status_code == 404


@requires_chromium
def test_e2e_applications_page_renders(app, mock_ats, client):
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1", title="SWE (page)", company="PageCorp", source="greenhouse")
    _run(app, job_id)
    html = client.get("/applications").text
    assert "Application tracker" in html
    assert "SWE (page)" in html
    assert "dry-run done" in html


@requires_chromium
def test_e2e_auto_submit_clicks_submit(app, mock_ats):
    """Agent mode: live run + auto_submit=True -> the runner clicks the form's
    submit button and records 'submitted' (mock ATS shows a thank-you page)."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1",
                               title="SWE (auto-submit)", company="AutoCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    assert result["status"] == "submitted", result

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        assert app_row.status == "submitted"
        actions = {l.action for l in db.query(ActionLog).filter_by(application_id=app_row.id)}
        assert "submitted" in actions
    finally:
        db.close()


@requires_chromium
def test_e2e_auto_submit_never_fires_in_dry_run(app, mock_ats):
    """Hard rule: dry-run + auto_submit must NOT submit anything."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1",
                               title="SWE (dry+auto)", company="DryCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=True, auto_submit=True)
    finally:
        db.close()
    assert result["status"] == "dry_run_completed", result

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        assert app_row.status == "dry_run_completed"
        actions = {l.action for l in db.query(ActionLog).filter_by(application_id=app_row.id)}
        assert "submitted" not in actions
        assert "auto_submit_skipped" in actions
    finally:
        db.close()


@requires_chromium
def test_e2e_tailored_resume_materialized_and_attached(app, mock_ats):
    """Vision step: tailor the resume to the JD, then the application run
    renders the generated resume to a real docx and attaches it to the form."""
    from backend.app.services.tailor import tailor_job

    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/1",
                               title="SWE (tailored)", company="TailorCo",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        res = tailor_job(db, job_id,
                         jd_text="Senior Java Spring Boot PostgreSQL microservices AWS engineer")
        tailored_id = res["tailored_resume_id"]
        job = db.get(Job, job_id)
        assert job.tailored_resume_id == tailored_id
        r = db.get(Resume, tailored_id)
        assert not r.storage_path  # generated: no file yet
    finally:
        db.close()

    result = _run(app, job_id)
    assert result["status"] == "dry_run_completed", result
    assert "resume_upload[0]" in result["filled_fields"], result["filled_fields"]

    db = app.state.SessionLocal()
    try:
        r = db.get(Resume, tailored_id)
        assert r.storage_path and os.path.exists(r.storage_path)
        assert r.original_filename.endswith(".docx")
    finally:
        db.close()
