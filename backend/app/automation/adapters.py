"""ATS adapters: Greenhouse, Lever, Ashby, Workday + shared heuristic filler.

Design: every adapter (1) tries its ATS-specific selectors, then (2) falls back
to the shared label-based heuristic so partially-customized forms still get
filled. Every value written comes from ApplicationContext (profile/resume
literals only — no generated text).
"""
from __future__ import annotations

import re

from .base import ApplicationContext, AtsAdapter, LogFn

# ---------------------------------------------------------------------------
# Shared heuristics
# ---------------------------------------------------------------------------

# field key -> regex applied to (name + id + placeholder + aria-label + label text)
FIELD_PATTERNS: list[tuple[str, str]] = [
    ("first_name", r"first[\s_-]*name|^fname$|given\s*name"),
    ("last_name", r"last[\s_-]*name|^lname$|surname|family[\s_-]*name"),
    ("full_name", r"^\s*(full[\s_-]*name|your[\s_-]*name|name)\s*$"),
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile|cell|\btel\b"),
    ("location", r"current\s*(address|location)|location|address|city|residen"),
    ("linkedin", r"linkedin"),
    ("portfolio", r"portfolio|personal\s*website|github|website"),
]

_CONSENT_PATTERNS = [
    (r"gdpr|data\s*protection|privacy\s*(policy|statement|notice)|consent", "consent"),
    (r"visa|sponsor|work\s*authorization|authorized", "work_auth"),
    (r"\beeo\b|equal\s*employment", "eeo"),  # US EEO checkboxes: skip, log only
    (r"referr|referral", "referral"),  # never fabricate a referral source
]


def _element_key(el) -> str:
    """Searchable key for an input/select/textarea: name/id/placeholder/aria/label."""
    parts = []
    for attr in ("name", "id", "placeholder", "aria-label"):
        try:
            v = el.get_attribute(attr)
        except Exception:
            v = None
        if v:
            parts.append(v)
    try:
        eid = el.get_attribute("id")
        if eid:
            label = el.locator(f"xpath=//label[contains(@for,'{eid}')]").first
            if label.count():
                txt = (label.inner_text() or "").strip()
                if txt:
                    parts.append(txt)
    except Exception:
        pass
    try:
        # <label><input></label> wrapper
        wrapped = el.locator("xpath=ancestor::label[1]").first
        if wrapped.count():
            txt = (wrapped.inner_text() or "").strip()
            if txt:
                parts.append(txt)
    except Exception:
        pass
    return " | ".join(parts).lower()


def _classify(key: str) -> str | None:
    for field_key, pattern in FIELD_PATTERNS:
        if re.search(pattern, key, re.IGNORECASE):
            return field_key
    return None


def _visible(el) -> bool:
    try:
        return el.is_visible()
    except Exception:
        return False


def _fill_text(el, value: str) -> None:
    """Type into an input/textarea (clear first), robust to React-controlled inputs."""
    el.click(timeout=3000)
    try:
        el.fill(value, timeout=3000)
    except Exception:
        el.press_sequentially(value, delay=10)


def fill_heuristic(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Fill every recognizable text/email/tel/url/textarea/select on the page.

    Returns labels of the fields filled. Conservative: unknown fields are left
    alone and logged as skipped (never guess free-text answers).
    """
    filled: list[str] = []
    ctx_map = {
        "first_name": ctx.first_name,
        "last_name": ctx.last_name,
        "full_name": ctx.full_name,
        "email": ctx.email,
        "phone": ctx.phone,
        "location": ctx.location,
        "linkedin": ctx.linkedin,
        "portfolio": ctx.portfolio,
    }
    try:
        els = page.locator(
            "input[type=text], input[type=email], input[type=tel], "
            "input[type=url], input:not([type]), textarea"
        )
    except Exception:
        return filled
    n = els.count()
    for i in range(n):
        el = els.nth(i)
        if not _visible(el):
            continue
        key = _element_key(el)
        if not key:
            continue
        field = _classify(key)
        if not field:
            log("skipped_field", key[:120], "unrecognized text field — left blank")
            continue
        value = ctx_map.get(field, "")
        if not value:
            log("skipped_field", field, "recognized but profile has no value")
            continue
        try:
            _fill_text(el, value)
        except Exception as e:  # keep going; one bad field must not kill the run
            log("field_error", field, f"{type(e).__name__}: {e}"[:200])
            continue
        filled.append(field)
        log("filled", field, value[:120])
    return filled


def fill_file_uploads(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Upload the resume into every visible file input (dedup by index)."""
    filled = []
    if not ctx.resume_path:
        log("skipped_field", "resume_upload", "no resume file available")
        return filled
    try:
        els = page.locator("input[type=file]")
    except Exception:
        return filled
    for i in range(els.count()):
        el = els.nth(i)
        if not _visible(el):
            continue
        try:
            el.set_input_files(ctx.resume_path, timeout=5000)
            filled.append(f"resume_upload[{i}]")
            log("uploaded_file", f"file[{i}]", ctx.resume_filename)
        except Exception as e:
            log("field_error", f"file[{i}]", f"{type(e).__name__}: {e}"[:200])
    return filled


def fill_consent(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Handle GDPR/EEO/work-auth checkboxes & radio groups by label semantics."""
    filled = []
    try:
        boxes = page.locator("input[type=checkbox], input[type=radio]")
    except Exception:
        return filled
    for i in range(boxes.count()):
        el = boxes.nth(i)
        if not _visible(el):
            continue
        key = _element_key(el)
        # also include the option's own label for radio groups
        try:
            label = el.locator("xpath=ancestor::label[1]").first
            if label.count():
                key += " | " + (label.inner_text() or "").strip().lower()
        except Exception:
            pass
        for pattern, kind in _CONSENT_PATTERNS:
            if re.search(pattern, key, re.IGNORECASE):
                if kind == "consent":
                    try:
                        if not el.is_checked():
                            el.check(timeout=3000)
                        filled.append(f"consent[{i}]")
                        log("consent_checked", key[:120], "privacy/GDPR consent")
                    except Exception as e:
                        log("field_error", f"consent[{i}]", f"{type(e).__name__}"[:120])
                elif kind == "work_auth":
                    _handle_work_auth(el, key, ctx, log, filled)
                elif kind == "eeo":
                    log("skipped_field", f"eeo[{i}]", "EEO optional question — left blank")
                elif kind == "referral":
                    log("skipped_field", f"referral[{i}]", "referral source — never fabricated")
                break
    return filled


# ---------------------------------------------------------------------------
# Screening questions: match stored (question, answer) pairs to form fields
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _question_text_for(el) -> str:
    """Collect candidate question text around a field: labels, legends, and the
    nearest preceding text blocks (Greenhouse puts questions in <p>/<label> above)."""
    parts: list[str] = []
    try:
        eid = el.get_attribute("id")
        if eid:
            lab = el.locator(f"xpath=//label[contains(@for,'{eid}')]").first
            if lab.count():
                parts.append(lab.inner_text() or "")
        wrapped = el.locator("xpath=ancestor::label[1]").first
        if wrapped.count():
            parts.append(wrapped.inner_text() or "")
        legend = el.locator("xpath=ancestor::fieldset[1]//legend[1]").first
        if legend.count():
            parts.append(legend.inner_text() or "")
    except Exception:
        pass
    # Nearest preceding text blocks: the field's own preceding sibling, then one level up
    node = el
    for _ in range(2):
        try:
            prev = node.locator("xpath=preceding-sibling::*[1]").first
            if prev.count():
                t = (prev.inner_text() or "").strip()
                if t and len(t) < 400:
                    parts.append(t)
            node = node.locator("xpath=..").first
            if not node.count():
                break
        except Exception:
            break
    return " ".join(dict.fromkeys(p for p in parts if p))


def _match_answer(question_text: str, answers: list[tuple[str, str]]) -> str | None:
    """Return the stored answer whose question best matches `question_text`.

    Recall-based: what fraction of the stored question's tokens appear in the
    field's surrounding text. Threshold 0.5. Never returns a guess below that.
    """
    q = _norm(question_text)
    q_tokens = set(q.split())
    if len(q_tokens) < 3:
        return None
    best: str | None = None
    best_score = 0.0
    for stored_q, answer in answers:
        s_tokens = set(_norm(stored_q).split())
        if len(s_tokens) < 2:
            continue
        score = len(q_tokens & s_tokens) / len(s_tokens)
        if score > best_score:
            best, best_score = answer, score
    return best if best_score >= 0.5 else None


def fill_screening_questions(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Fill text fields whose surrounding question text matches a stored answer.

    Fields with no stored match are left blank and logged as
    'unanswered_question' — the caller (or the user) must supply the answer.
    """
    filled: list[str] = []
    if not ctx.answers:
        return filled
    try:
        els = page.locator("textarea, input[type=text], input:not([type])")
    except Exception:
        return filled
    for i in range(els.count()):
        el = els.nth(i)
        if not _visible(el):
            continue
        key = _element_key(el)
        if _classify(key):  # identity/contact fields are handled elsewhere
            continue
        qtext = _question_text_for(el)
        if not qtext:
            continue
        answer = _match_answer(qtext, ctx.answers)
        if not answer:
            log("unanswered_question", (key or qtext)[:120],
                f"no stored answer matches — left blank (ask user): {qtext[:100]}")
            continue
        try:
            _fill_text(el, answer)
            filled.append("screening_question")
            log("filled", "screening_question", f"Q≈{qtext[:80]} | A≈{answer[:80]}")
        except Exception as e:
            log("field_error", "screening_question", f"{type(e).__name__}"[:120])
    return filled


def _handle_work_auth(el, key: str, ctx: ApplicationContext, log: LogFn, filled: list) -> None:
    """Pick the H-1B / sponsorship option when the profile needs sponsorship."""
    low = key.lower()
    if ctx.sponsorship_needed and ctx.region == "us":
        if re.search(r"h-?1b|sponsor", low):
            try:
                el.check(timeout=3000)
                filled.append(f"work_auth[{low[:40]}]")
                log("work_auth", "h1b", "selected sponsorship/H-1B option")
                return
            except Exception:
                pass
        if re.search(r"no\s|not\s.*needed|not\s*required|no\s*visa", low):
            log("skipped_field", "work_auth", "avoided 'no sponsorship' option (profile needs H-1B)")
            return
        log("skipped_field", "work_auth", f"no H-1B-labeled option matched: {low[:80]}")
    else:
        log("skipped_field", "work_auth", f"region={ctx.region} — left for manual review")


# ---------------------------------------------------------------------------
# ATS-specific adapters
# ---------------------------------------------------------------------------


class GreenhouseAdapter(AtsAdapter):
    name = "greenhouse"

    @classmethod
    def detect(cls, url: str) -> bool:
        u = url.lower()
        return "greenhouse.io" in u or "job-boards.greenhouse.io" in u

    def fill(self, page, ctx: ApplicationContext, log: LogFn) -> list[str]:
        filled: list[str] = []
        # Greenhouse standard field names
        mapping = {
            "first_name": "input[name='fields[first_name]']",
            "last_name": "input[name='fields[last_name]']",
            "email": "input[name='fields[email]']",
            "phone": "input[name='fields[phone]']",
        }
        for field, sel in mapping.items():
            value = getattr(ctx, field, "")
            if not value:
                continue
            try:
                el = page.locator(sel).first
                if el.count() and _visible(el):
                    _fill_text(el, value)
                    filled.append(field)
                    log("filled", field, value[:120])
            except Exception as e:
                log("field_error", field, f"{type(e).__name__}"[:120])
        filled += fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log)
        filled += fill_file_uploads(page, ctx, log)
        filled += fill_consent(page, ctx, log)
        log("adapter", "greenhouse", f"filled {len(filled)} fields")
        return filled


class LeverAdapter(AtsAdapter):
    name = "lever"

    @classmethod
    def detect(cls, url: str) -> bool:
        u = url.lower()
        return "lever.co" in u or "apply.lever.co" in u or "jobs.lever.co" in u

    def fill(self, page, ctx: ApplicationContext, log: LogFn) -> list[str]:
        filled: list[str] = []
        mapping = {
            "first_name": "input#firstName, input[name=firstName]",
            "last_name": "input#lastName, input[name=lastName]",
            "email": "input#email, input[name=email]",
            "phone": "input#phone, input[name=phone]",
            "location": "input#currentLocation, input[name=currentLocation]",
        }
        for field, sel in mapping.items():
            value = getattr(ctx, field, "")
            if not value:
                continue
            try:
                el = page.locator(sel).first
                if el.count() and _visible(el):
                    _fill_text(el, value)
                    filled.append(field)
                    log("filled", field, value[:120])
            except Exception as e:
                log("field_error", field, f"{type(e).__name__}"[:120])
        filled += fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log)
        filled += fill_file_uploads(page, ctx, log)
        filled += fill_consent(page, ctx, log)
        log("adapter", "lever", f"filled {len(filled)} fields")
        return filled


class AshbyAdapter(AtsAdapter):
    name = "ashby"

    @classmethod
    def detect(cls, url: str) -> bool:
        u = url.lower()
        return "ashbyhq.com" in u or "jobs.ashbyhq.com" in u or "apply.workable" not in u and "ashby" in u

    def fill(self, page, ctx: ApplicationContext, log: LogFn) -> list[str]:
        # Ashby forms are React-heavy and mostly custom-labeled: heuristic-first
        filled = fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log)
        filled += fill_file_uploads(page, ctx, log)
        filled += fill_consent(page, ctx, log)
        log("adapter", "ashby", f"filled {len(filled)} fields (heuristic-first)")
        return filled


class WorkdayAdapter(AtsAdapter):
    name = "workday"

    @classmethod
    def detect(cls, url: str) -> bool:
        u = url.lower()
        return (
            "workday.com" in u
            or "wday.com" in u
            or "myworkdayjobs.com" in u
        )

    def fill(self, page, ctx: ApplicationContext, log: LogFn) -> list[str]:
        log("adapter_note", "workday", "experimental — Workday SPA; best-effort heuristic fill")
        filled = fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log)
        filled += fill_file_uploads(page, ctx, log)
        filled += fill_consent(page, ctx, log)
        return filled


_ADAPTERS: list[type[AtsAdapter]] = [
    GreenhouseAdapter,
    LeverAdapter,
    AshbyAdapter,
    WorkdayAdapter,
]

GREENHOUSE = GreenhouseAdapter()
LEVER = LeverAdapter()
ASHBY = AshbyAdapter()
WORKDAY = WorkdayAdapter()


def detect_adapter(url: str, hint: str = "") -> AtsAdapter | None:
    """Return the first adapter whose URL signature matches, else the adapter
    matching `hint` (e.g. the job's ingest source — useful when the apply URL
    is not on a recognizable ATS domain). Else None."""
    for cls in _ADAPTERS:
        try:
            if cls.detect(url or ""):
                return cls()
        except Exception:
            continue
    hint = (hint or "").strip().lower()
    for cls in _ADAPTERS:
        if hint and cls.name == hint:
            return cls()
    return None
