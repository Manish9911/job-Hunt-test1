"""Application runner: drives Playwright, logs every action.

Flow per run:
  open apply_url -> captcha check -> ATS detection -> adapter fill
  -> screenshot -> [auto-submit when auto_submit=True] -> store Application
  + ActionLog rows.

Submit policy:
  - dry-run (default): nothing is ever submitted.
  - live, no auto_submit: form is filled + screenshotted; a human clicks submit.
  - live + auto_submit (the agent): the runner clicks the form's own submit
    button and records the outcome. Hard rules stay: NEVER solve a CAPTCHA
    (a blocked form is logged + skipped, never bypassed) and never fill data
    outside the profile/answers the user provided.

Statuses:
  dry_run_completed        form filled + screenshotted, nothing submitted
  ready_for_submission     filled, human must click submit
  submitted                auto-submit clicked, success indicators seen
  submit_uncertain         auto-submit clicked, outcome unconfirmed — review
  submit_blocked_captcha   form filled, CAPTCHA guards submission — human step
  submit_failed            submit button not found / click error
  captcha_manual           bot challenge found before filling — human step
  manual_required          no adapter matched the URL — apply by hand
  failed                   technical failure (timeout, crash)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout

from ..db.models import ActionLog, Application, Job, Profile, Resume
from .adapters import detect_adapter
from .base import ApplicationContext, CaptchaDetected, check_captcha


def screenshots_dir() -> Path:
    d = Path("data") / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_context(db, profile: Profile, resume: Resume | None,
                  answers: list[tuple[str, str]] | None = None) -> ApplicationContext:
    ctx = ApplicationContext.from_profile(profile, resume=resume)
    if answers is not None:
        ctx.answers = list(answers)
    return ctx


def _resume_for_job(db, job: Job, resume_id: int | None) -> Resume:
    if resume_id:
        r = db.get(Resume, resume_id)
        if r:
            return r
    if job.tailored_resume_id:
        r = db.get(Resume, job.tailored_resume_id)
        if r:
            return r
    profile_id = _profile_id_of_job(db, job)
    if profile_id:
        master = (
            db.query(Resume).filter(Resume.profile_id == profile_id, Resume.is_master.is_(True))
            .first()
        )
        if master:
            return master
    raise ValueError("No resume available for this job (no tailored, no master)")


def _profile_id_of_job(db, job: Job) -> int | None:
    # Job has no direct profile link; use the single profile in the system.
    p = db.query(Profile).first()
    return p.id if p else None


def _find_profile(db) -> Profile | None:
    return db.query(Profile).first()


def _find_submit_button(page):
    """Return the most plausible visible submit control, or None.

    A page with no form fields at all cannot have a valid submit button —
    that's how the Valinor incident happened: the job *posting* page's
    "Apply for this Job" CTA was treated as submit.
    """
    try:
        has_form = (
            page.locator("input:visible").count()
            + page.locator("select:visible").count()
            + page.locator("textarea:visible").count()
        ) > 0
    except Exception:
        has_form = True
    if not has_form:
        return None
    selectors = (
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit application")',
        'button:has-text("Submit")',
        'button:has-text("Complete and submit")',
        'button:has-text("Apply now")',
    )
    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 6)):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_enabled():
                    return el
            except Exception:
                continue
    return None


_APPLY_CTA_RE = re.compile(
    r"apply for this job|apply for this position|"
    r"start application|begin application", re.I)


def _form_inputs_visible(page) -> bool:
    """True when the page shows real form fields (i.e. the application form
    is open — as opposed to a read-only job posting)."""
    try:
        loc = page.locator("input:visible, select:visible, textarea:visible")
        for i in range(min(loc.count(), 20)):
            el = loc.nth(i)
            try:
                tag = (el.evaluate("el => el.tagName") or "").upper()
                t = (el.get_attribute("type") or "text").lower() if tag == "INPUT" else "text"
            except Exception:
                continue
            if t not in ("submit", "button", "hidden", "reset"):
                return True
        return False
    except Exception:
        return False


def _open_application_if_needed(page, log) -> None:
    """Some ATS URLs are the JOB POSTING page — the form only appears after
    clicking the "Apply for this Job" CTA. Do that step before filling, so
    the adapter finds real form fields instead of a read-only JD page.

    Guardrails (Valinor incident): never touch a *submit-type* button here,
    and only look for CTAs when no form fields are visible.
    """
    if _form_inputs_visible(page):
        return  # form already open
    try:
        cands = page.locator("button, a[role='button'], input[type='button']")
        for i in range(min(cands.count(), 30)):
            el = cands.nth(i)
            try:
                if not (el.is_visible() and el.is_enabled()):
                    continue
                if (el.get_attribute("type") or "").lower() == "submit":
                    continue  # a form submit button — never an "open" CTA
                text = (el.inner_text(timeout=500) or "").strip()
            except Exception:
                continue
            if text and _APPLY_CTA_RE.search(text):
                log("open_application", None, f"clicking CTA: {text[:60]!r}")
                el.click(timeout=10000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeout:
                    pass
                try:
                    page.wait_for_selector(
                        'input[type="email"], input[type="text"]', timeout=10000)
                except PWTimeout:
                    pass
                log("application_form_opened", None,
                    "application form is open — filling it now")
                return
    except Exception as e:
        log("open_application_failed", None, f"{type(e).__name__}: {str(e)[:120]}")


def _auto_submit(page, log) -> tuple[str, str]:
    """Click the form's submit button and judge the outcome.

    Returns ``(status, note)`` with status one of: submitted,
    submit_uncertain, submit_blocked_captcha, submit_failed.
    Hard rules: never solve a CAPTCHA; never re-type anything.
    """
    try:
        check_captcha(page)
    except CaptchaDetected as e:
        log("submit_blocked_captcha", None, e.evidence)
        return "submit_blocked_captcha", f"CAPTCHA guards submission: {e.evidence}"

    btn = _find_submit_button(page)
    if btn is None:
        log("submit_button_not_found", None, "no visible submit control — review manually")
        return "submit_failed", "no submit button found — review the filled form and submit manually"

    try:
        before_url = page.url
        btn.click(timeout=10000)
    except Exception as e:
        log("submit_click_error", None, f"{type(e).__name__}"[:200])
        return "submit_failed", f"submit click failed: {type(e).__name__}"

    # Give the ATS time to process + navigate to a confirmation page.
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(2500)
    except Exception:
        pass

    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        body = ""
    try:
        new_url = page.url
    except Exception:
        new_url = before_url

    success_markers = (
        "thank", "thank you", "application has been received",
        "we've received", "we have received", "successfully submitted",
        "you're all set", "application received", "next steps",
        "what happens next", "confirmation",
    )
    body_changed = new_url.rstrip("/") != before_url.rstrip("/")
    has_marker = any(m in body for m in success_markers)
    form_still_there = False
    try:
        form_still_there = page.locator('form input[type="email"]').count() > 0
    except Exception:
        pass

    if has_marker or (body_changed and not form_still_there):
        log("submitted", None, f"success indicators seen (url_changed={body_changed}, marker={has_marker})")
        return "submitted", "submit clicked — confirmation indicators seen"
    log("submit_uncertain", None, f"clicked, but no confirmation (url_changed={body_changed})")
    return "submit_uncertain", "submit clicked — no confirmation page detected, verify with the employer"


def _ensure_resume_file(db, resume) -> None:
    """Materialize a generated (tailored) resume as a real docx.

    Tailored resumes are stored as structured JSON (``content_json``) with an
    empty ``storage_path``; the ATS adapters need an actual file to attach.
    """
    if resume.storage_path:
        return
    if not resume.content_json:
        raise ValueError("resume has no file on disk and no parsed content to render")
    from ..services.export import export_docx

    structure = json.loads(resume.content_json)
    out_dir = Path("data") / "resumes"
    path = out_dir / f"resume_{resume.id}.docx"
    export_docx(structure, path)
    resume.storage_path = str(path)
    if not resume.original_filename or resume.original_filename.startswith("generated"):
        resume.original_filename = path.name
    db.commit()


def run_application(db, job_id: int, resume_id: int | None = None,
                    dry_run: bool = True, headless: bool = True,
                    nav_timeout_ms: int = 60000,
                    auto_submit: bool = False) -> dict:
    """Run one application attempt. Synchronous — call from a thread.

    ``auto_submit=True`` (used by the auto-apply agent) makes the runner click
    the form's submit button after filling. It never solves CAPTCHAs and never
    submits when the run is a dry-run.
    """
    from playwright.sync_api import sync_playwright

    job = db.get(Job, job_id)
    if not job:
        return {"error": "job not found", "status": "failed"}
    if not job.apply_url:
        return {"error": "job has no apply_url", "status": "failed"}

    profile = _find_profile(db)
    if not profile:
        return {"error": "no profile configured", "status": "failed"}

    try:
        resume = _resume_for_job(db, job, resume_id)
    except ValueError as e:
        return {"error": str(e), "status": "failed"}

    # Generated (tailored) resumes have no file on disk yet — render one.
    try:
        _ensure_resume_file(db, resume)
    except Exception as e:
        return {"error": f"could not prepare resume file: {e}", "status": "failed"}

    answers = []
    try:
        from ..db.models import CommonAnswer

        for ca in db.query(CommonAnswer).filter(CommonAnswer.profile_id == profile.id).all():
            answers.append((ca.question, ca.answer))
    except Exception:
        pass
    ctx = build_context(db, profile, resume, answers)

    app = Application(job_id=job.id, resume_id=resume.id, dry_run=bool(dry_run),
                      status="pending", created_at=datetime.utcnow())
    db.add(app)
    db.commit()
    db.refresh(app)
    app_id = app.id

    def log(action: str, field_name: str | None, detail: str | None) -> None:
        db.add(ActionLog(application_id=app_id, job_id=job.id,
                         action=action, field=field_name, detail=detail))
        db.commit()

    # AI drafter for random screening questions (user rule: humanized answers,
    # no AI footprints). Active only when an LLM is configured; otherwise
    # unknown questions stay blank + logged, exactly as before.
    drafter = None
    try:
        from ..config import Settings
        from ..llm import get_llm
        from ..services import answer as answer_svc
        from ..services.tailor import fetch_jd, render_resume_text

        llm = get_llm(Settings())
        if llm.is_available():
            structure = json.loads(resume.content_json or "{}")
            resume_text = render_resume_text(structure)
            try:
                jd_text = fetch_jd(job)
            except Exception:
                jd_text = ""
            drafter = answer_svc.make_drafter(
                applicant_name=profile.full_name,
                job_title=job.title,
                company=job.company,
                jd_text=jd_text,
                resume_text=resume_text,
                saved_answers=answers,
                llm=llm,
            )
            if drafter is not None:
                log("ai_drafter_ready", None,
                    f"random screening questions get AI-drafted, fact-checked answers ({llm.name})")
        else:
            log("ai_drafter_off", None, "no LLM configured — unknown questions stay blank")
    except Exception as e:
        drafter = None
        log("ai_drafter_unavailable", None, f"{type(e).__name__}: {str(e)[:120]}")

    status = "dry_run_completed" if dry_run else "ready_for_submission"
    error_text: str | None = None
    filled: list[str] = []
    screenshot_path: str | None = None
    notes: dict = {"resume": resume.original_filename, "resume_id": resume.id}

    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/124.0 Safari/537.36"),
                )
                page = context.new_page()
                log("navigate", None, job.apply_url[:250])
                page.goto(job.apply_url, wait_until="domcontentloaded",
                          timeout=nav_timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass  # some ATSes stream forever; DOM state is enough

                try:
                    check_captcha(page)
                except CaptchaDetected as e:
                    # Evidence shot for the human who must finish this step
                    try:
                        shot = screenshots_dir() / f"app_{job.id}_{datetime.utcnow():%Y%m%d_%H%M%S}_captcha.png"
                        page.screenshot(path=str(shot), full_page=True)
                        screenshot_path = str(shot)
                        log("screenshot", None, shot.name + " (captcha page)")
                    except Exception:
                        pass
                    raise
                adapter = detect_adapter(job.apply_url, hint=getattr(job, "source", "") or "")
                if adapter is None:
                    status = "manual_required"
                    log("adapter_none", None,
                        f"no adapter for {job.apply_url[:120]} — apply manually")
                    notes["adapter"] = None
                else:
                    notes["adapter"] = adapter.name
                    log("adapter_matched", adapter.name, job.apply_url[:120])
                    _open_application_if_needed(page, log)
                    # User rule: if the form offers "Autofill from resume",
                    # upload the resume there and let the ATS fill it.
                    from .adapters import autofill_from_resume
                    autofill_from_resume(page, ctx, log)
                    filled = adapter.fill(page, ctx, log, drafter=drafter)
                    notes["filled_fields"] = filled
                    if not filled:
                        log("fill_warning", None, "no fields were filled — custom form?")
                    # Let post-fill network activity settle (form autosave, analytics)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except PWTimeout:
                        pass

                shot = screenshots_dir() / f"app_{job.id}_{datetime.utcnow():%Y%m%d_%H%M%S}.png"
                try:
                    page.screenshot(path=str(shot), full_page=True)
                    screenshot_path = str(shot)
                    log("screenshot", None, shot.name)
                except Exception as e:
                    log("screenshot_error", None, f"{type(e).__name__}"[:120])

                if auto_submit and not dry_run:
                    status, submit_note = _auto_submit(page, log)
                    notes["submit"] = submit_note
                    # Truth guard (Valinor incident): "submitted" requires that
                    # a form was actually filled. Zero fields + URL change is
                    # just the apply CTA opening the form — not a submission.
                    if status == "submitted" and not filled:
                        status = "submit_uncertain"
                        notes["submit"] += (" — CORRECTION: no form fields were "
                                             "filled, so nothing was submitted; "
                                             "the click only opened the "
                                             "application. Re-run or finish manually.")
                        log("submit_guard", None,
                            "no fields filled — demoting submitted -> submit_uncertain")
                elif auto_submit and dry_run:
                    log("auto_submit_skipped", None, "dry-run — nothing submitted")
            finally:
                browser.close()
    except CaptchaDetected as e:
        status = "captcha_manual"
        error_text = f"CAPTCHA / bot challenge: {e.evidence}. A human must complete this step."
        log("captcha_detected", None, e.evidence)
        notes["captcha"] = e.evidence
    except PWTimeout:
        status = "failed"
        error_text = "Navigation timed out — try again or apply manually."
        log("nav_timeout", None, f"no load within {nav_timeout_ms}ms")
    except Exception as e:
        status = "failed"
        error_text = f"{type(e).__name__}: {e}"[:400]
        log("error", None, error_text)

    app.status = status
    app.screenshot_path = screenshot_path
    app.error_text = error_text
    app.notes_json = json.dumps(notes, default=str)[:8000]
    app.finished_at = datetime.utcnow()
    db.commit()

    log_count = (
        db.query(ActionLog).filter(ActionLog.application_id == app_id).count()
    )
    return {
        "application_id": app_id,
        "job_id": job.id,
        "status": status,
        "error": error_text,
        "filled_fields": filled,
        "screenshot_path": screenshot_path,
        "screenshot_url": f"/api/applications/{app_id}/screenshot" if screenshot_path else None,
        "adapter": notes.get("adapter"),
        "resume_used": resume.original_filename,
        "log_entries": log_count,
        "detail_url": f"/applications/{app_id}",
    }
