"""Application runner: drives Playwright, logs every action, never submits.

Flow per run:
  open apply_url -> captcha check -> ATS detection -> adapter fill
  -> screenshot -> store Application + ActionLog rows.

Statuses:
  dry_run_completed   form filled + screenshotted, nothing submitted (default)
  ready_for_submission  same, but requested with dry_run=False — human must
                        still open the form and click submit themselves
  captcha_manual      bot challenge found — a human must finish this step
  manual_required     no adapter matched the URL — apply by hand (link provided)
  failed              technical failure (timeout, crash)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

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


def run_application(db, job_id: int, resume_id: int | None = None,
                    dry_run: bool = True, headless: bool = True,
                    nav_timeout_ms: int = 60000) -> dict:
    """Run one (dry-run) application attempt. Synchronous — call from a thread."""
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
                    filled = adapter.fill(page, ctx, log)
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
