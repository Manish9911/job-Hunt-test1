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
    """Return the most plausible visible FINAL submit control, or None.

    A page with no form fields at all cannot have a valid submit button —
    that's how the Valinor incident happened: the job *posting* page's
    "Apply for this Job" CTA was treated as submit.

    Multi-step forms (Qualtrics et al.): "Continue"/"Next" are often
    type=submit buttons too. Clicking one of those is NOT a submission —
    so when a step-advance button is visible, only an explicit final-submit
    label counts.
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

    def _form_input_count(el) -> int:
        """Visible real inputs inside the candidate button's form (-1 = none).

        Used to separate the application form's submit button from small
        side forms on the same page (Qualtrics' posting/form pages carry a
        'Get alerts' email box with its own SUBMIT button — that must never
        be treated as the application's final submit).
        """
        try:
            return el.evaluate(
                "el => { const f = el.form; if (!f) return -1;"
                " return [...f.querySelectorAll('input, select, textarea')]"
                ".filter(i => i.offsetParent !== null && i.type !== 'hidden').length; }")
        except Exception:
            return -1

    def _main_form_count(page) -> int:
        try:
            return page.evaluate(
                "() => { let best = 0;"
                " for (const f of document.querySelectorAll('form')) {"
                "   const n = [...f.querySelectorAll('input, select, textarea')]"
                ".filter(i => i.offsetParent !== null && i.type !== 'hidden').length;"
                "   if (n > best) best = n; } return best; }")
        except Exception:
            return 0

    main_count = _main_form_count(page)

    def _is_application_submit(el) -> bool:
        # No form on the page at all (JS-driven ATS) → any labeled button is
        # the best candidate. Otherwise it must belong to the biggest form.
        if main_count <= 0:
            return True
        return _form_input_count(el) >= main_count

    def _first_visible(sel: str):
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:
            return None
        for i in range(min(n, 6)):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_enabled() and _is_application_submit(el):
                    return el
            except Exception:
                continue
        return None

    # 1) Explicit final-submit labels — always the right choice.
    for sel in (
        'button:has-text("Submit application")',
        'button:has-text("Submit Application")',
        'button:has-text("Submit my application")',
        'button:has-text("Complete and submit")',
        'button:has-text("Submit")',
        'input[type="submit"]',
        'button:has-text("Apply now")',
    ):
        el = _first_visible(sel)
        if el is not None:
            return el

    # 2) Bare type=submit fallback — but only on forms that have NO
    #    step-advance button (otherwise "Continue" would be a false submit).
    try:
        advance = page.locator(
            'button:visible, input[type="submit"]:visible')
        has_advance = False
        for i in range(min(advance.count(), 10)):
            e = advance.nth(i)
            try:
                t = (e.inner_text(timeout=300) or "").strip()
            except Exception:
                t = ""
            if t and _STEP_CTA_RE.search(t):
                has_advance = True
                break
        if not has_advance:
            el = _first_visible('button[type="submit"]')
            if el is not None:
                return el
    except Exception:
        pass
    return None


_APPLY_CTA_RE = re.compile(
    r"apply for this job|apply for this position|apply now|apply here|"
    r"click here to apply|start application|begin application", re.I)

# Multi-step forms (Qualtrics/Avature et al.): "Continue" / "Next" buttons
# advance a step; only an explicit final-submit label ends the application.
_STEP_CTA_RE = re.compile(r"^\s*(continue|next|continue to next step)\s*$", re.I)


def _count_form_inputs(page) -> int:
    try:
        loc = page.locator("input:visible, select:visible, textarea:visible")
        n = 0
        for i in range(min(loc.count(), 40)):
            el = loc.nth(i)
            try:
                tag = (el.evaluate("el => el.tagName") or "").upper()
                t = (el.get_attribute("type") or "text").lower() if tag == "INPUT" else "text"
            except Exception:
                continue
            if t not in ("submit", "button", "hidden", "reset"):
                n += 1
        return n
    except Exception:
        return 0


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


_CLEARANCE_FORM_RE = re.compile(
    r"security clearance"
    r"|u\.s\. (?:security )?clearance"
    r"|clearance eligib\w*"
    r"|\bdo you (?:hold|have) [\w ]{0,25}clearance\b"
    r"|\bare you eligible[\w ]{0,25}clearance\b",
    re.I)


def _clearance_question_on_form(page) -> str | None:
    """User rule (2026-11-03): if the form asks about security clearance,
    abort immediately — the user is not authorized for clearance work and
    doesn't want time wasted on those postings. Returns a short quote."""
    try:
        body = re.sub(r"\s+", " ", (page.inner_text("body") or ""))
    except Exception:
        return None
    m = _CLEARANCE_FORM_RE.search(body)
    if not m:
        return None
    start = max(0, m.start() - 40)
    return body[start: min(len(body), m.end() + 60)].strip()


# User rule (2026-11-09, job-apply-agent safety model): NEVER pay a fee. A
# legitimate application form never asks for card details or money. Checked
# on the OPEN form (not just the JD) right before filling: a hit aborts the
# run — nothing is typed, nothing is submitted. The regex lives in
# services.fraud so the detector has a single home.
from ..services.fraud import scan_payment_in_text


def _payment_question_on_form(page) -> str | None:
    try:
        body = page.inner_text("body") or ""
    except Exception:
        return None
    return scan_payment_in_text(body)


def _open_application_if_needed(page, log) -> bool:
    """Some ATS URLs are the JOB POSTING page — the form only appears after
    clicking the "Apply now" / "Apply for this Job" CTA. Do that step before
    filling, so the adapter finds real form fields instead of a read-only JD
    page.

    Guardrails (Valinor incident): never touch a *submit-type* button here;
    the CTA text must be an explicit apply phrase; and a page that clearly
    IS a form (file input, or >= 6 real fields) is left alone — posting pages
    may carry small alert/cookie-box inputs, which must not be mistaken for
    an open form (Qualtrics' posting page has an "alert me" email box).

    Returns False when a CTA was found but could not be opened — the caller
    must then NOT fill the page (it would land on posting-page widgets like
    the alert box's email field).
    """
    cta = None
    cta_text = ""
    try:
        cands = page.locator("button, a[role='button'], input[type='button'], a")
        n = min(cands.count(), 40)
        for i in range(n):
            el = cands.nth(i)
            try:
                if not (el.is_visible() and el.is_enabled()):
                    continue
                if (el.get_attribute("type") or "").lower() == "submit":
                    continue  # a form submit button — never an "open" CTA
                text = (el.inner_text(timeout=400) or "").strip()
            except Exception:
                continue
            if not (text and len(text) < 60 and _APPLY_CTA_RE.search(text)):
                continue
            # A bare <a> must also look like an apply link (extra safety:
            # nav/footer links that happen to say "Apply now" elsewhere).
            try:
                tag = (el.evaluate("el => el.tagName") or "").upper()
            except Exception:
                tag = ""
            if tag == "A":
                href = (el.get_attribute("href") or "").lower()
                if not re.search(r"apply|job|position|career", href):
                    continue
            cta, cta_text = el, text
            break
    except Exception as e:
        log("open_application_failed", None, f"{type(e).__name__}: {str(e)[:120]}")
        return False
    if cta is None:
        return True
    # Clearly a real form already? Don't click CTAs on top of it.
    try:
        if page.locator("input[type=file]:visible").count() > 0:
            return True
        if _count_form_inputs(page) >= 6:
            return True
    except Exception:
        pass
    try:
        log("open_application", None, f"clicking CTA: {cta_text[:60]!r}")
        _click_robust(cta, log, "apply CTA")
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
        return True
    except Exception as e:
        log("open_application_failed", None, f"{type(e).__name__}: {str(e)[:120]}")
        return False


def _click_robust(loc, log, what: str) -> None:
    """Click a button. If an overlay (sticky header, chat widget, cookie
    banner) swallows the pointer and the click times out, fall back to an
    element-level (synthetic) click — it still fires the element's
    navigation/handlers even when a transparent div sits on top.
    """
    try:
        loc.click(timeout=10000)
    except Exception:
        loc.evaluate("el => el.click()")
        log("js_click_fallback", what,
            "pointer click blocked (overlay?) — used element-level click")


def _settle_before_click(page) -> None:
    """Defocus the active field and let blur-triggered layout changes settle
    BEFORE clicking a button.

    Why: filling a field leaves it focused. When the submit click's mousedown
    moves focus away, the field's blur handler can show a validation message
    inline — which shifts the button under the cursor. The mouseup then lands
    on the message, not the button, and the form never submits (the click
    event fires on the common ancestor — no button activation, no error).
    JS-level clicks are unaffected, which is why the form looked fine.
    """
    try:
        page.evaluate(
            "(() => { const a = document.activeElement; "
            "if (a && a !== document.body && a.blur) a.blur(); })()")
    except Exception:
        pass
    try:
        page.wait_for_timeout(300)
    except Exception:
        pass


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
        _settle_before_click(page)
        _click_robust(btn, log, "submit")
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


def _ensure_resume_file(db, resume, job=None) -> None:
    """Materialize a generated (tailored) resume as a real docx.

    Tailored resumes are stored as structured JSON (``content_json``) with an
    empty ``storage_path``; the ATS adapters need an actual file to attach.

    Naming (user rule 2026-11-06): "<user> <company> <role>.docx" — no
    ``resume_<id>`` numbers, no underscores/symbols.
    """
    if resume.storage_path:
        return
    if not resume.content_json:
        raise ValueError("resume has no file on disk and no parsed content to render")
    from ..services.export import export_docx, humanized_filename

    structure = json.loads(resume.content_json)
    out_dir = Path("data") / "resumes"
    filename = humanized_filename(
        structure.get("name"),
        getattr(job, "company", None),
        getattr(job, "title", None),
        ext="docx",
    )
    path = out_dir / filename
    export_docx(structure, path)
    resume.storage_path = str(path)
    resume.original_filename = filename
    db.commit()


_MAX_STEPS = 6


def _step_signature(page) -> str:
    """Cheap fingerprint of the current form step (for no-progress detection)."""
    try:
        n = page.evaluate("""() => [...document.querySelectorAll('input, select, textarea')]
            .filter(e => e.offsetParent !== null).length""")
    except Exception:
        n = -1
    try:
        url = page.url
    except Exception:
        url = ""
    return f"{url}|{n}"


def _find_step_button(page, regex: "re.Pattern[str]"):
    """First visible button/input whose trimmed text matches exactly."""
    try:
        loc = page.locator('button:visible, input[type="submit"]:visible')
        for i in range(min(loc.count(), 15)):
            el = loc.nth(i)
            try:
                t = (el.inner_text(timeout=300) or "").strip()
            except Exception:
                t = ""
            if t and regex.search(t):
                return el
    except Exception:
        pass
    return None


def _fill_all_steps(page, adapter, ctx, log, drafter=None) -> list[str]:
    """Fill the current step, then advance multi-step forms ("Continue"/
    "Next") until the final step with the real submit button is reached.

    Returns the union of filled fields across all steps. Stops safely when:
    the final submit button is visible, there is no step-advance button,
    the form stops advancing (validation errors — e.g. a field the user
    never answered), or MAX_STEPS is hit.
    """
    filled: list[str] = []
    last_sig = None
    for step in range(1, _MAX_STEPS + 1):
        filled += adapter.fill(page, ctx, log, drafter=drafter)
        if _find_submit_button(page) is not None:
            log("final_step_reached", None,
                f"step {step}: final submit button visible — multi-step fill done")
            break
        sig = _step_signature(page)
        cont = _find_step_button(page, _STEP_CTA_RE)
        if cont is None:
            log("single_step_form", None,
                "no Continue/Next and no final submit — single-step form")
            break
        if last_sig is not None and sig == last_sig:
            log("step_blocked", None,
                f"step {step} did not advance (validation?) — stopping; "
                "human may need to finish the form")
            break
        last_sig = sig
        try:
            _settle_before_click(page)
            _click_robust(cont, log, f"step {step} Continue")
            log("step_advance", None, f"step {step}: clicked Continue — next step")
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PWTimeout:
                pass
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
        except Exception as e:
            log("step_advance_failed", None, f"{type(e).__name__}: {str(e)[:120]}")
            break
    else:
        log("max_steps_reached", None, f"stopped after {_MAX_STEPS} steps")
    return filled


def run_application(db, job_id: int, resume_id: int | None = None,
                    dry_run: bool = True, headless: bool = True,
                    nav_timeout_ms: int = 60000,
                    auto_submit: bool = False,
                    existing_application_id: int | None = None) -> dict:
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
        _ensure_resume_file(db, resume, job)
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

    if existing_application_id is not None:
        # Approval flow (2026-11-09): the staged row becomes the real attempt,
        # so one job keeps one row: staged -> submitting -> submitted.
        app = db.get(Application, existing_application_id)
        if app is None or app.job_id != job.id:
            raise ValueError("existing application row does not belong to this job")
        app.status = "submitting"
        app.dry_run = bool(dry_run)
    else:
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
                clearance_hit = _clearance_question_on_form(page)
                if clearance_hit:
                    # User rule 2026-11-03: skip clearance jobs immediately —
                    # nothing is filled, nothing is submitted.
                    status = "rejected_clearance"
                    log("rejected_clearance", None,
                        f"form asks about security clearance: {clearance_hit[:140]} — skipped (user is not authorized for clearance work)")
                    notes["clearance"] = clearance_hit[:200]

                # Never-pays-a-fee rule (2026-11-09): check the landing page
                # too, before any CTA/form work.
                payment_hit = None if clearance_hit else _payment_question_on_form(page)
                if payment_hit:
                    status = "submit_blocked_payment"
                    log("submit_blocked_payment", None,
                        f"page demands payment: {payment_hit[:140]} — ABORTED (hard rule: never pay a fee; nothing filled or submitted)")
                    notes["payment"] = payment_hit[:200]

                adapter = (None if (clearance_hit or payment_hit)
                           else detect_adapter(job.apply_url, hint=getattr(job, "source", "") or ""))
                filled = []
                form_ready = True
                if adapter is None and not clearance_hit and not payment_hit:
                    status = "manual_required"
                    log("adapter_none", None,
                        f"no adapter for {job.apply_url[:120]} — apply manually")
                    notes["adapter"] = None
                elif adapter is not None:
                    notes["adapter"] = adapter.name
                    log("adapter_matched", adapter.name, job.apply_url[:120])
                    form_ready = _open_application_if_needed(page, log)
                    if not form_ready:
                        status = "manual_required"
                        log("open_application_aborted", None,
                            "could not open the application form (CTA click blocked) — apply manually")
                    # User rule: if the form offers "Autofill from resume",
                    # upload the resume there and let the ATS fill it.
                    from .adapters import autofill_from_resume
                    if form_ready:
                        autofill_from_resume(page, ctx, log)
                    # User rule (2026-11-04): the clearance question can hide
                    # BEHIND the "Apply for this Job" CTA (Valinor's real form
                    # does this) — the pre-CTA check can't see it. Re-check
                    # once the form is open, BEFORE filling anything: if the
                    # form asks about security clearance, abort — no filling,
                    # no submitting, no time wasted.
                    clearance_hit = _clearance_question_on_form(page)
                    # Never-pays-a-fee rule: checked on the open form before
                    # a single field is typed (2026-11-09).
                    payment_hit = None if clearance_hit else _payment_question_on_form(page)
                    if clearance_hit:
                        status = "rejected_clearance"
                        log("rejected_clearance", None,
                            f"form asks about security clearance: {clearance_hit[:140]} — skipped (user is not authorized for clearance work)")
                        notes["clearance"] = clearance_hit[:200]
                    elif payment_hit:
                        status = "submit_blocked_payment"
                        log("submit_blocked_payment", None,
                            f"form demands payment: {payment_hit[:140]} — ABORTED (hard rule: never pay a fee; nothing filled or submitted)")
                        notes["payment"] = payment_hit[:200]
                    elif not form_ready:
                        pass  # CTA could not be opened — nothing to fill (logged above)
                    else:
                        filled = _fill_all_steps(page, adapter, ctx, log, drafter=drafter)
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

                if payment_hit and not form_ready:
                    pass  # payment demand on the form — already aborted above
                elif auto_submit and not dry_run and not clearance_hit and not payment_hit and form_ready:
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
