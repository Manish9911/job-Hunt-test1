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
          company: str = "MockCorp", source: str = "unknown",
          phone: str | None = None) -> tuple[int, int]:
    """Create profile + master resume + job. Returns (profile_id, job_id)."""
    Session = app.state.SessionLocal
    db = Session()
    try:
        kw = dict(PROFILE_KW)
        if phone is not None:
            kw["phone"] = phone
        profile = Profile(**kw)
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
    import re as _re

    class FakePage:
        def __init__(self, html):
            self._html = html
            self._body = _re.sub(r"<[^>]+>", " ", html)

        def content(self):
            return self._html

        def inner_text(self, _sel):
            return self._body

        def locator(self, _sel):
            raise NotImplementedError("fake page has no rendered widgets")

    # A fully rendered page is fine — even if its source references the
    # recaptcha API (Qualtrics' careers pages do, for their chatbot).
    check_captcha(FakePage(
        "<html><body><h1>Software Engineer at Qualtrics</h1>"
        "<p>We are looking for an engineer to join our data platform team in "
        "Seattle. You will build and operate large-scale data pipelines, "
        "services and dashboards that power customer experience products used "
        "by thousands of companies. Strong Java or Python background, "
        "experience with cloud platforms, SQL and observability. Hybrid work "
        "model with competitive salary and benefits, equity and 401k.</p>"
        "<a href='/apply'>Apply now</a>"
        "</body></html>"))
    check_captcha(FakePage(
        "<html><body><h1>Job posting</h1>"
        "<p>Real content: we are looking for a software engineer with solid "
        "experience in Java, Spring and cloud platforms to join our data "
        "platform team. You will design APIs, keep services healthy and "
        "reliable, and work with product managers to ship features every "
        "week. The team values ownership, clear communication and a strong "
        "bias toward shipping. Salary range is competitive for the level.</p>"
        "<script src='https://www.google.com/recaptcha/api.js'></script>"
        "</body></html>"))

    for html, label in (
        ("<h1>Just a moment...</h1>", "challenge text"),
        ("<p>Please verify you are human</p>", "challenge text"),
        # Near-empty page + challenge markers in source = interstitial.
        ('<div class="g-recaptcha" data-sitekey="abc"></div>', "interstitial"),
        ('<script src="https://hcaptcha.com/1/api.js"></script>', "interstitial"),
        ("<div class='cf-turnstile'></div>", "interstitial"),
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
    if (!el || (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA' && el.tagName !== 'SELECT')) return;
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

# Job POSTING page (like jobs.ashbyhq.com/<co>/<id>): read-only JD + an
# "Apply for this Job" CTA that opens the actual form. The Valinor incident
# was a run that treated this CTA as the submit button.
POSTING_HTML = """
<html><body>
<h1>Senior Software Engineer — MockCorp</h1>
<p>We are looking for a Java/Spring engineer.</p>
<button onclick="window.location.href='/greenhouse/posting/apply'">Apply for this Job</button>
</body></html>
"""

# Form with an "Autofill from resume" flow: uploading the resume makes the
# ATS populate the fields with the resume's (different!) data.
AUTOFILL_HTML = """
<html><body><form method="post" action="/submit">
  <label>First name <input name="fields[first_name]"></label>
  <label>Last name <input name="fields[last_name]"></label>
  <label>Email <input name="fields[email]"></label>
  <label>Phone <input name="fields[phone]"></label>
  <label>Resume <input type="file" name="resume" id="autofill-file"></label>
  <button type="button" id="autofill-btn">Autofill from resume</button>
  <p>Why are you interested in this role?</p>
  <textarea name="motivation"></textarea>
  <label><input type="checkbox" name="gdpr_consent"> I consent to the processing of my personal data (GDPR)</label>
  <button type="submit">Submit application</button>
</form>
<script>
  // Simulate the ATS autofill: on resume upload, populate from the resume.
  document.getElementById('autofill-file').addEventListener('change', function () {
    if (!this.files || !this.files.length) return;
    var vals = {'fields[first_name]': 'Auto', 'fields[last_name]': 'Filled',
                'fields[email]': 'autofilled@example.com',
                'fields[phone]': '+1 555 0100'};
    for (var name in vals) {
      var el = document.querySelector('input[name="' + name + '"]');
      el.value = vals[name];
      el.dispatchEvent(new Event('change', {bubbles: true}));
    }
  });
  document.getElementById('autofill-btn').addEventListener('click', function () {
    document.getElementById('autofill-file').click();
  });
</script>
</body></html>
"""

def _form_html(body_html: str, form_name: str) -> str:
    return body_html + _CAPTURE_JS.replace("FORM", form_name)


FORMS = {
    "/greenhouse/1": _form_html(GREENHOUSE_HTML, "greenhouse"),
    "/lever/1": _form_html(LEVER_HTML, "lever"),
    "/captcha": CAPTCHA_HTML,
    "/unknown": UNKNOWN_HTML,
    "/greenhouse/posting": POSTING_HTML,
    "/greenhouse/posting/apply": _form_html(GREENHOUSE_HTML, "posting"),
    "/greenhouse/autofill": _form_html(AUTOFILL_HTML, "autofill"),
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
    assert "staged — your call" in html


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


@requires_chromium
def test_submit_button_ignored_on_job_posting_page(app, mock_ats):
    """Regression (Valinor incident): a job POSTING page has no form fields —
    its 'Apply for this Job' CTA must never be treated as the submit button."""
    from playwright.sync_api import sync_playwright
    from backend.app.automation.runner import _find_submit_button

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(f"{mock_ats}/greenhouse/posting", wait_until="domcontentloaded")
        # sanity: the CTA is right there, tempting
        assert page.locator("button:has-text('Apply for this Job')").count() == 1
        assert _find_submit_button(page) is None
        b.close()


@requires_chromium
def test_e2e_posting_page_cta_open_form_fill_submit(app, mock_ats):
    """Regression (Valinor incident): when apply_url is the job POSTING page,
    the runner clicks the 'Apply for this Job' CTA first, fills the form that
    opens, and only then submits — and 'submitted' requires filled fields."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/posting",
                               title="SWE (posting CTA)", company="CTACorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    assert result["status"] == "submitted", result
    # the form behind the CTA must have been actually filled
    assert result["filled_fields"], "no fields filled — CTA click was treated as submit?"

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        actions = {l.action for l in db.query(ActionLog).filter_by(application_id=app_row.id)}
        assert "open_application" in actions          # CTA clicked
        assert "application_form_opened" in actions
        assert "submitted" in actions
    finally:
        db.close()


@requires_chromium
def test_e2e_autofill_from_resume_used_when_offered(app, mock_ats):
    """User rule (2026-11-03): when the form offers 'Autofill from resume',
    the resume is uploaded THERE and the ATS's own data populates the form —
    and the field-by-field filler must NOT clobber the autofilled values."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/autofill",
                               title="SWE (autofill)", company="AutoFillCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    assert result["status"] == "submitted", result

    # The form captured the RESUME's data (autofill), not the profile's.
    cap = _captures("autofill")
    assert cap.get("fields[email]") == "autofilled@example.com", cap
    assert cap.get("fields[first_name]") == "Auto", cap

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        rows = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in rows}
        assert "autofill_upload" in actions, actions
        assert "autofill_applied" in actions, actions
        # the heuristic filler kept the autofilled values instead of typing over them
        assert any(l.action == "skipped_field" and "already filled" in (l.detail or "")
                   for l in rows)
        assert "submitted" in actions
    finally:
        db.close()


# Form containing a security-clearance question (like Valinor's real form):
# "Clearance Eligibility" radio buttons. User rule 2026-11-03: if the form
# asks this, abort the whole application — nothing filled, nothing submitted.
CLEARANCE_HTML = """
<html><body><form method="post" action="/submit">
  <label>First name <input name="fields[first_name]"></label>
  <label>Email <input name="fields[email]"></label>
  <label>Phone <input name="fields[phone]"></label>
  <p>Clearance Eligibility: Do you hold an active U.S. security clearance?</p>
  <label><input type="radio" name="clearance" value="yes"> Yes</label>
  <label><input type="radio" name="clearance" value="no"> No</label>
  <button type="submit">Submit application</button>
</form></body></html>
"""

FORMS["/greenhouse/clearance"] = _form_html(CLEARANCE_HTML, "clearance")


@requires_chromium
def test_e2e_clearance_question_aborts_application(app, mock_ats):
    """User rule (2026-11-03): a form that asks about security clearance must
    abort the application immediately — the user is not authorized for
    clearance work and doesn't want time wasted on those postings."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/clearance",
                               title="SWE (clearance)", company="ClearanceCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    assert result["status"] == "rejected_clearance", result
    assert result["filled_fields"] == [], result

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        notes = app_row.notes_json or "{}"
        assert "clearance" in notes, notes
        rows = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in rows}
        assert "rejected_clearance" in actions, actions
        assert "submitted" not in actions
        assert "autofill_upload" not in actions
    finally:
        db.close()
    # Nothing was typed into the form or POSTed to the mock ATS.
    cap = _captures("clearance")
    assert cap.get("fields[email]") in (None, ""), cap


# Job POSTING page whose form (behind the CTA) asks about security clearance —
# exactly what Valinor's real Ashby form does: the posting page is clean, the
# "Clearance Eligibility" question only appears AFTER clicking the CTA.
POSTING_CLEARANCE_HTML = """
<html><body>
<h1>Senior Software Engineer — ClearCorp</h1>
<p>We are looking for a Java/Spring engineer.</p>
<button onclick="window.location.href='/greenhouse/posting-clearance/apply'">Apply for this Job</button>
</body></html>
"""

FORMS["/greenhouse/posting-clearance"] = POSTING_CLEARANCE_HTML
FORMS["/greenhouse/posting-clearance/apply"] = _form_html(CLEARANCE_HTML, "posting-clearance")


@requires_chromium
def test_e2e_clearance_question_behind_cta_aborts(app, mock_ats):
    """User rule (2026-11-04): the clearance question can hide BEHIND the
    'Apply for this Job' CTA — the pre-CTA check can't see it. The runner
    must re-check once the form is open and abort before filling anything:
    no fields filled, nothing submitted, application marked
    rejected_clearance."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/posting-clearance",
                               title="SWE (clearance behind CTA)", company="ClearBehindCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    assert result["status"] == "rejected_clearance", result
    assert result["filled_fields"] == [], result

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        notes = app_row.notes_json or "{}"
        assert "clearance" in notes, notes
        rows = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in rows}
        # The CTA WAS clicked (form opened) — then the clearance question was
        # detected on the form and the run aborted.
        assert "open_application" in actions, actions
        assert "application_form_opened" in actions, actions
        assert "rejected_clearance" in actions, actions
        assert "submitted" not in actions
    finally:
        db.close()
    # Nothing was typed into the form or POSTed to the mock ATS.
    cap = _captures("posting-clearance")
    assert cap.get("fields[email]") in (None, ""), cap


# ---------------------------------------------------------------------------
# Multi-step forms (Qualtrics/Avature et al.): "Continue" advances a step;
# only an explicit final submit ends the application.
# ---------------------------------------------------------------------------
MULTISTEP1_HTML = """
<html><body>
<form>
  <label>First name <input name="fields[first_name]"></label>
  <label>Email <input type="email" name="fields[email]"></label>
  <button type="submit" onclick="event.preventDefault(); window.location.href='/greenhouse/multistep/2'">Continue</button>
</form>
"""
MULTISTEP2_HTML = """
<html><body>
<form method="post" action="/submit">
  <p>Why are you interested in this role?</p>
  <textarea name="fields[motivation]"></textarea>
  <button type="submit">Submit application</button>
</form>
"""

FORMS["/greenhouse/multistep/1"] = _form_html(MULTISTEP1_HTML, "multistep1")
FORMS["/greenhouse/multistep/2"] = _form_html(MULTISTEP2_HTML, "multistep2")


@requires_chromium
def test_e2e_multistep_continue_until_final_submit(app, mock_ats):
    """Multi-step form: fill step 1, click Continue, fill step 2, and only
    then submit. 'Continue' (a type=submit button) must never be treated as
    the final submit."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/multistep/1",
                               title="SWE (multi-step)", company="MultiStepCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    import time; time.sleep(0.8)  # let mock capture POSTs land
    assert result["status"] == "submitted", result
    # Step-1 fields were filled before advancing.
    assert any(f.startswith("first_name") for f in result["filled_fields"]), result["filled_fields"]
    assert any(f.startswith("email") for f in result["filled_fields"]), result["filled_fields"]

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        rows = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = [l.action for l in rows]
        assert "step_advance" in actions, actions
        assert "final_step_reached" in actions, actions
        assert actions.index("step_advance") < actions.index("final_step_reached")
    finally:
        db.close()
    # Step 2 is what actually got POSTed.
    cap = _captures("multistep2")
    assert cap.get("fields[motivation]"), cap


# Resume upload via a BUTTON that opens a native file chooser (Qualtrics).
UPLOAD_BTN_HTML = """
<html><body><form method="post" action="/submit">
  <label>Email <input type="email" name="fields[email]"></label>
  <button type="button" id="up">Upload Resume</button>
  <button type="submit">Submit application</button>
</form>
<script>
  document.getElementById('up').addEventListener('click', function () {
    var f = document.createElement('input');
    f.type = 'file';
    document.body.appendChild(f);
    f.click();
  });
</script>
</body></html>
"""

FORMS["/greenhouse/upload-btn"] = _form_html(UPLOAD_BTN_HTML, "upload-btn")


@requires_chromium
def test_e2e_resume_upload_via_button(app, mock_ats):
    """No visible file input, but an 'Upload Resume' button that opens a file
    chooser — the resume must be attached through it."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/upload-btn",
                               title="SWE (upload btn)", company="UploadBtnCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    import time; time.sleep(0.8)
    assert result["status"] == "submitted", result
    assert "resume_upload[button]" in result["filled_fields"], result["filled_fields"]

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        rows = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in rows}
        assert "uploaded_file" in actions, actions
    finally:
        db.close()


# Phone validation: the stored format is rejected, digits-only is accepted.
PHONE_RETRY_HTML = """
<html><body><form method="post" action="/submit">
  <label>Phone <input id="ph" name="fields[phone]"></label>
  <span class="err" id="pherr" style="display:none">Please enter a valid phone number</span>
  <button type="submit">Submit application</button>
</form>
<script>
  var ph = document.getElementById('ph');
  ph.addEventListener('blur', function () {
    document.getElementById('pherr').style.display = /^\\d{10}$/.test(ph.value) ? 'none' : 'block';
  });
</script>
</body></html>
"""

FORMS["/greenhouse/phone-retry"] = _form_html(PHONE_RETRY_HTML, "phone-retry")


@requires_chromium
def test_e2e_phone_format_retry_digits_only(app, mock_ats):
    """Stored phone '+1 555-867-5309' is rejected by a 10-digit validator —
    the runner retries with US digits-only ('5558675309') after seeing the
    validation error. Same number, new format."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/phone-retry",
                               title="SWE (phone fmt)", company="PhoneFmtCorp",
                               source="greenhouse", phone="+1 555-867-5309")
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    import time; time.sleep(0.8)
    assert result["status"] == "submitted", result

    db = app.state.SessionLocal()
    try:
        app_row = db.query(Application).filter_by(job_id=job_id).first()
        rows = db.query(ActionLog).filter_by(application_id=app_row.id).all()
        actions = {l.action for l in rows}
        assert "phone_format_retry" in actions, actions
    finally:
        db.close()
    # What actually got POSTed is the accepted 10-digit format.
    cap = _captures("phone-retry")
    assert cap.get("fields[phone]") == "5558675309", cap


# Qualtrics step-4 style: dropdowns, a select-all checkbox group, a yes/no
# radio group, and a follow-up text field that must NOT swallow a stored
# answer meant for a different question (the live-run false positive).
SCREENING_MIXED_HTML = """
<html><body><form method="post" action="/submit">
  <p>Are you of legal age to work in the country in which this position is based?</p>
  <select name="fields[legal_age]">
    <option value="">Please select</option>
    <option value="yes">Yes</option>
    <option value="no">No</option>
  </select>
  <p>Are you now or will you in the future in need of sponsorship?</p>
  <select name="fields[sponsorship]">
    <option value="">Please select</option>
    <option value="yes">Yes</option>
    <option value="no">No</option>
  </select>
  <p>Are you legally authorized to work in the United States?</p>
  <select name="fields[work_auth]">
    <option value="">Please select</option>
    <option value="yes">Yes</option>
    <option value="yes-sponsor">Yes, but I require sponsorship now or in the future</option>
    <option value="no">No</option>
  </select>
  <fieldset><legend>Where have you seen this company prior to applying? Select all that apply.</legend>
    <label><input type="checkbox" name="fields[source_1]"> I googled jobs and a role was suggested</label>
    <label><input type="checkbox" name="fields[source_2]"> I learned about it from a current employee</label>
    <label><input type="checkbox" name="fields[source_3]"> None of the above</label>
  </fieldset>
  <fieldset><legend>Are you related to a current employee of this company?</legend>
    <label><input type="radio" name="fields[related]" value="yes"> Yes</label>
    <label><input type="radio" name="fields[related]" value="no"> No</label>
  </fieldset>
  <p>If you answered yes to being related to a current employee, please specify name and relationship (N/A if not applicable).</p>
  <input type="text" name="fields[related_detail]">
  <button type="submit">Submit application</button>
</form>
"""

FORMS["/greenhouse/screening-mix"] = _form_html(SCREENING_MIXED_HTML, "screening-mix")


@requires_chromium
def test_e2e_screening_selects_and_checkbox_groups(app, mock_ats):
    """Dropdowns and checkbox/radio groups are answered ONLY from stored
    answers: the sponsorship-aware option is picked for work-authorization,
    the matching 'seen company' option is ticked, and everything without a
    stored answer (legal age, related-to-employee, follow-up text field) is
    left blank — never fabricated."""
    profile_id, job_id = _seed(app, f"{mock_ats}/greenhouse/screening-mix",
                               title="SWE (screening)", company="ScreenMixCorp",
                               source="greenhouse")
    db = app.state.SessionLocal()
    try:
        db.add(CommonAnswer(profile_id=profile_id,
                            question="Where have you seen this company prior to applying?",
                            answer="I googled jobs and a role was suggested"))
        db.add(CommonAnswer(profile_id=profile_id,
                            question="Are you now or in the future in need of sponsorship?",
                            answer="Yes — US work-authorization sponsorship is required."))
        db.commit()
    finally:
        db.close()
    db = app.state.SessionLocal()
    try:
        result = run_application(db, job_id, dry_run=False, auto_submit=True)
    finally:
        db.close()
    import time; time.sleep(0.8)
    assert result["status"] == "submitted", result

    cap = _captures("screening-mix")
    # work-authorization: stored 'Yes, with H-1B sponsorship.' → sponsored yes
    assert cap.get("fields[work_auth]") == "yes-sponsor", cap
    # sponsorship dropdown: stored yes → plain 'Yes' (no sponsor option exists)
    assert cap.get("fields[sponsorship]") == "yes", cap
    # no stored answer → left untouched (no fabricated choices)
    assert "fields[legal_age]" not in cap, cap
    assert "fields[related]" not in cap, cap
    # checkbox group: only the option matching the stored answer is ticked
    assert cap.get("fields[source_1]") == "true", cap
    assert "fields[source_2]" not in cap, cap
    assert "fields[source_3]" not in cap, cap
    # the follow-up text field must NOT swallow the sponsorship answer
    # (regression: live Qualtrics run filled it with the wrong text)
    assert "fields[related_detail]" not in cap, cap
