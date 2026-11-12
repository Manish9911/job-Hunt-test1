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
    (r"gdpr|data\s*protection|privacy\s*(policy|statement|notice)|consent"
     r"|\bi (?:agree|consent)\b", "consent"),
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


def _already_filled(el) -> bool:
    """True when the field already holds a value — e.g. populated by the
    ATS's 'Autofill from resume' (user rule 2026-11-03: resume data wins)."""
    try:
        return bool((el.input_value(timeout=300) or "").strip())
    except Exception:
        return False


def _fill_text(el, value: str) -> None:
    """Type into an input/textarea (clear first), robust to React-controlled
    inputs. Email fields are TYPED character-by-character: some ATS forms
    (Qualtrics) wipe fast-filled email values during blur validation — a
    human-like keystroke sequence is what their React state reliably sees."""
    el.click(timeout=3000)
    try:
        ftype = (el.get_attribute("type") or "").lower()
    except Exception:
        ftype = ""
    if ftype == "email" or "@" in value:
        try:
            el.fill("", timeout=2000)
        except Exception:
            pass
        el.press_sequentially(value, delay=15)
        return
    try:
        el.fill(value, timeout=3000)
    except Exception:
        el.press_sequentially(value, delay=10)


def _phone_format_retry(el, value: str, log: LogFn) -> None:
    """Some ATS forms reject the stored phone format ('+1 555-867-5309' vs
    US 10-digit '5558675309'). Blur to trigger validation; if an error shows
    up, retry with digits-only (US 10-digit convention — same number, new
    format, still the user's phone)."""
    try:
        el.dispatch_event("blur")
        page = getattr(el, "page", None)
        if page is not None:
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        err = el.evaluate("""el => {
            let n = el.parentElement;
            for (let i = 0; i < 3 && n; i++) {
                const e = n.querySelector('[class*=err], [role=alert]');
                if (e && e.offsetParent !== null && e.innerText && e.innerText.trim()) return e.innerText.trim().slice(0, 80);
                n = n.parentElement;
            }
            return null;
        }""")
        if err:
            digits = re.sub(r"\D", "", value)
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]  # drop the US country code
            digits = digits[-10:]
            if digits and len(digits) >= 7 and digits != value:
                el.fill(digits)
                try:
                    el.dispatch_event("blur")
                except Exception:
                    pass
                log("phone_format_retry", "phone",
                    f"form rejected {value!r} ({err!r}) — retried as {digits!r}")
    except Exception:
        pass


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
        # Never clobber a field that's already filled — e.g. by the ATS's
        # "Autofill from resume" (user rule 2026-11-03): the resume's own
        # data wins over the profile's.
        try:
            if _already_filled(el):
                log("skipped_field", field, "already filled (resume autofill?) — kept")
                # A phone that was filled by an earlier loop still needs the
                # format retry: the stored format may be rejected by the form
                # (e.g. '+1 555-867-5309' vs US 10-digit '5558675309').
                if field == "phone":
                    try:
                        _phone_format_retry(el, el.input_value(timeout=200) or "", log)
                    except Exception:
                        pass
                continue
        except Exception:
            pass
        value = ctx_map.get(field, "")
        if not value:
            log("skipped_field", field, "recognized but profile has no value")
            continue
        try:
            _fill_text(el, value)
        except Exception as e:  # keep going; one bad field must not kill the run
            log("field_error", field, f"{type(e).__name__}: {e}"[:200])
            continue
        if field == "phone":
            _phone_format_retry(el, value, log)
        filled.append(field)
        log("filled", field, value[:120])
    return filled


def fill_file_uploads(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Upload the resume into every visible file input (dedup by index).

    Some forms (Qualtrics/Avature) expose upload only through a button that
    opens a native file chooser — handled via expect_file_chooser.
    """
    filled = []
    if not ctx.resume_path:
        log("skipped_field", "resume_upload", "no resume file available")
        return filled
    try:
        els = page.locator("input[type=file]")
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
    except Exception:
        pass
    # Button that opens a file chooser ("Upload Resume" et al.).
    try:
        btn = None
        locs = page.locator("button, [role='button']")
        for i in range(min(locs.count(), 40)):
            el = locs.nth(i)
            if not _visible(el):
                continue
            try:
                text = (el.inner_text(timeout=300) or "").strip()
            except Exception:
                continue
            if text and len(text) < 60 and re.search(
                    r"upload (your )?resume|attach (your )?resume|resume upload",
                    text, re.I):
                btn = el
                break
        if btn is not None and ctx.resume_path:
            with page.expect_file_chooser(timeout=6000) as fc:
                btn.click(timeout=5000)
            fc.value.set_files(ctx.resume_path)
            filled.append("resume_upload[button]")
            log("uploaded_file", "upload_resume_button", ctx.resume_filename)
    except Exception as e:
        log("field_error", "resume_upload_button", f"{type(e).__name__}: {str(e)[:120]}")
    return filled


_AUTOFILL_CTA_RE = re.compile(
    r"autofill\s+from\s+(my\s+|the\s+|your\s+)?resume|"
    r"auto-?fill\s+(my\s+)?resume|"
    r"import\s+(my\s+)?resume|pre-?fill\s+from\s+resume|fill\s+from\s+resume",
    re.I)


def _nonempty_text_fields(page) -> int:
    try:
        els = page.locator(
            "input[type=text], input[type=email], input[type=tel], "
            "input[type=url], input:not([type]), textarea")
        n = 0
        for i in range(min(els.count(), 40)):
            try:
                if (els.nth(i).input_value(timeout=200) or "").strip():
                    n += 1
            except Exception:
                pass
        return n
    except Exception:
        return 0


def autofill_from_resume(page, ctx: ApplicationContext, log: LogFn) -> bool:
    """User rule (2026-11-03): when the form offers 'Autofill from resume',
    upload the resume THERE and let the ATS populate the form — it's more
    accurate than field-by-field typing. If no such control/flow exists, do
    nothing and the normal fill pipeline (profile + stored answers + AI
    drafter) takes over.

    Returns True when the resume was handed to the ATS's autofill path.
    """
    if not ctx.resume_path:
        return False

    def _has_autofill_control() -> bool:
        try:
            for sel in ("button", "a", "[role='button']", "label", "span"):
                loc = page.locator(sel)
                for i in range(min(loc.count(), 40)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        text = (el.inner_text(timeout=300) or "").strip()
                    except Exception:
                        continue
                    if text and len(text) < 80 and _AUTOFILL_CTA_RE.search(text):
                        return True
        except Exception:
            pass
        return False

    before = _nonempty_text_fields(page)

    # Pattern A: a (possibly hidden) file input — uploading triggers the ATS.
    try:
        files = page.locator("input[type=file]")
        if files.count():
            first = files.nth(0)
            try:
                first.evaluate("el => { el.style.display='block'; el.style.visibility='visible'; }")
            except Exception:
                pass
            first.set_input_files(ctx.resume_path, timeout=8000)
            log("autofill_upload", None,
                f"resume {ctx.resume_filename} handed to the form's autofill/file input")
            # Give the ATS time to parse + populate.
            for _ in range(6):
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    break
                if _nonempty_text_fields(page) > before:
                    log("autofill_applied", None,
                        "ATS populated the form from the resume")
                    return True
    except Exception as e:
        log("autofill_upload_failed", None, f"{type(e).__name__}: {str(e)[:120]}")

    # Pattern B: a labeled button that opens a file chooser.
    if _has_autofill_control():
        try:
            with page.expect_file_chooser(timeout=8000) as fc:
                clicked = False
                for sel in ("button", "a", "[role='button']"):
                    loc = page.locator(sel)
                    for i in range(min(loc.count(), 40)):
                        el = loc.nth(i)
                        try:
                            if not el.is_visible():
                                continue
                            text = (el.inner_text(timeout=300) or "").strip()
                        except Exception:
                            continue
                        if text and _AUTOFILL_CTA_RE.search(text):
                            el.click(timeout=5000)
                            clicked = True
                            break
                    if clicked:
                        break
                if not clicked:
                    raise RuntimeError("autofill control not clickable")
            fc.value.set_files(ctx.resume_path)
            log("autofill_upload", None,
                f"resume {ctx.resume_filename} chosen via the autofill button")
            for _ in range(6):
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    break
                if _nonempty_text_fields(page) > before:
                    log("autofill_applied", None,
                        "ATS populated the form from the resume")
                    return True
        except Exception as e:
            log("autofill_button_failed", None, f"{type(e).__name__}: {str(e)[:120]}")

    log("autofill_unavailable", None,
        "no working 'Autofill from resume' path — falling back to field-by-field fill")
    return False


def _tick_checkbox(el, page=None):
    """Check a checkbox. Styled checkboxes (Qualtrics/Avature) visually hide
    the raw <input> — clicking it does nothing, so fall back to the
    associated label (for=/aria-labelledby), a wrapping <label>, the parent
    element, and finally a DOM-level click. Raises if it never ticks."""
    try:
        el.check(timeout=3000)
        return
    except Exception:
        pass
    page = page or getattr(el, "page", None)
    eid = None
    try:
        eid = el.get_attribute("id")
    except Exception:
        eid = None
    candidates = []
    if eid:
        try:
            candidates.append(el.locator(f"xpath=//label[@for='{eid}']").first)
        except Exception:
            pass
        try:
            al = el.get_attribute("aria-labelledby")
            if al:
                candidates.append(el.locator(f"xpath=//*[@id='{al}']").first)
        except Exception:
            pass
    try:
        candidates.append(el.locator("xpath=ancestor::label[1]").first)
    except Exception:
        pass
    try:
        candidates.append(el.locator("xpath=..").first)
    except Exception:
        pass
    for cand in candidates:
        try:
            if not cand.count():
                continue
            cand.click(timeout=3000)
            if el.is_checked():
                return
        except Exception:
            continue
    try:
        el.evaluate("el => el.click()")
        if el.is_checked():
            return
    except Exception:
        pass
    raise RuntimeError("checkbox would not tick (styled control?)")


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
                            _tick_checkbox(el)
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


_STOPWORDS = {
    "a", "an", "the", "are", "is", "was", "were", "be", "been", "being", "am",
    "you", "your", "yours", "i", "me", "my", "mine", "we", "us", "our", "ours",
    "he", "she", "they", "them", "their", "his", "her", "its", "it",
    "to", "of", "in", "on", "at", "by", "with", "for", "from", "and", "or",
    "but", "if", "then", "than", "so", "as", "do", "does", "did", "doing",
    "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "please", "what", "which", "who", "whom", "whose", "when", "where", "how",
    "why", "not", "no", "yes", "and", "up", "down", "out", "over", "under",
    "again", "once", "here", "there", "all", "any", "both", "each", "few",
    "more", "most", "other", "some", "such", "only", "same", "too", "very",
    "just", "about", "into", "through", "during", "before", "after", "above",
    "below", "between", "while", "also", "now", "this", "that", "these", "those",
}


def _stem(t: str) -> str:
    """Crude suffix stem so 'sponsorship' and 'sponsor' compare equal."""
    if t.endswith("ship") and len(t) > 5:
        return t[:-4]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    if t.endswith("ing") and len(t) > 5:
        return t[:-3]
    if t.endswith("ed") and len(t) > 4:
        return t[:-2]
    return t


def _content_tokens(s: str) -> set[str]:
    """Lowercase content tokens (function words stripped, suffix-stemmed) — the
    words that actually identify a question ('sponsor', 'notice', 'period')."""
    out: set[str] = set()
    for t in _norm(s).split():
        if t and t not in _STOPWORDS and len(t) > 1:
            out.add(_stem(t))
    return out


def _question_text_for(el) -> str:
    """Collect candidate question text around a field: labels, legends, and
    the nearest preceding text blocks — walking up to 4 levels and stopping
    as soon as a question-mark text is found, so a field never absorbs the
    question belonging to the field above it (live Qualtrics bug: the
    related-employee dropdown matched the education question above it).
    """
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
    if "?" in " ".join(parts):
        return " ".join(dict.fromkeys(p for p in parts if p))[:400]
    node = el
    for _ in range(6):
        try:
            got = False
            prev = node.locator("xpath=preceding-sibling::*[1]").first
            if prev.count():
                t = (prev.inner_text() or "").strip()
                if t and len(t) < 400:
                    parts.append(t)
                    got = True
            node = node.locator("xpath=..").first
            if not node.count():
                break
            if got and "?" in (parts[-1] if parts else ""):
                break
        except Exception:
            break
    return " ".join(dict.fromkeys(p for p in parts if p))[:400]


def _match_answer(question_text: str, answers: list[tuple[str, str]]) -> str | None:
    """Return the stored answer whose question best matches `question_text`.

    Content-token recall: what fraction of the stored question's *content*
    tokens (function words stripped) appear in the field's surrounding text.
    Threshold 0.5 AND at least two distinct content tokens must match — a
    single coincidental word is never enough. This is what kept the
    'visa sponsorship' answer from being glued onto an unrelated
    'related to a current employee?' follow-up (they shared only 'now'/'or').
    """
    q_tokens = _content_tokens(question_text)
    if len(q_tokens) < 2:
        return None
    best: str | None = None
    best_score = 0.0
    best_len = 0
    for stored_q, answer in answers:
        s_tokens = _content_tokens(stored_q)
        if len(s_tokens) < 2:
            continue
        hit = q_tokens & s_tokens
        if len(hit) < 2:
            continue
        score = len(hit) / len(s_tokens)
        # Tie-break: prefer the more specific (longer) stored question — it
        # identifies the question better than a shorter sibling of it.
        if score > best_score or (score == best_score and len(s_tokens) > best_len):
            best, best_score, best_len = answer, score, len(s_tokens)
    return best if best_score >= 0.5 else None


def fill_screening_questions(page, ctx: ApplicationContext, log: LogFn,
                             drafter=None) -> list[str]:
    """Fill text fields whose surrounding question text matches a stored answer.

    ``drafter`` (optional): callable(question_text) -> answer | None. Used for
    questions with no stored answer — an AI-drafted, truth-checked, humanized
    response (see services.answer). Fields that end up with neither a stored
    answer nor a draft are left blank and logged as 'unanswered_question'.
    """
    filled: list[str] = []
    if not ctx.answers and drafter is None:
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
        ai_draft = False
        if not answer and drafter is not None:
            try:
                answer = drafter(qtext)
            except Exception as e:
                answer = None
                log("ai_draft_error", (key or qtext)[:120], f"{type(e).__name__}"[:120])
            ai_draft = bool(answer)
        if not answer:
            log("unanswered_question", (key or qtext)[:120],
                f"no stored answer matches — left blank (ask user): {qtext[:100]}")
            continue
        try:
            _fill_text(el, answer)
            filled.append("screening_question")
            if ai_draft:
                log("ai_draft_answer", (key or qtext)[:120],
                    f"Q≈{qtext[:80]} | A≈{answer[:140]} (AI-drafted, fact-checked — review before submit)")
            else:
                log("filled", "screening_question", f"Q≈{qtext[:80]} | A≈{answer[:80]}")
        except Exception as e:
            log("field_error", "screening_question", f"{type(e).__name__}"[:120])
    return filled


# ---------------------------------------------------------------------------
# Screening dropdowns & checkbox groups (Qualtrics/Avature multi-step forms)
# ---------------------------------------------------------------------------


def _pick_option_value(select_el, answer: str) -> str | None:
    """Choose the <option> that best matches a stored answer.

    Plain yes/no answers map to the option starting with that word — a yes
    mentioning sponsorship prefers the sponsored variant, a 'no sponsorship
    needed' answer prefers the plain yes. Text answers are matched by
    content-token coverage of each option. Returns the option's label text.
    """
    try:
        opts = select_el.locator("option")
        texts: list[str] = []
        for i in range(opts.count()):
            t = (opts.nth(i).inner_text() or "").strip()
            if t:
                texts.append(t)
    except Exception:
        return None
    if not texts:
        return None
    low = answer.lower().strip()
    if low in ("yes", "y") or re.match(r"yes\b", low):
        yes_opts = [t for t in texts if t.lower().startswith("yes")]
        if not yes_opts:
            return None
        if re.search(r"sponsor|h-?1b|\bvisa\b", low):
            if re.search(r"(no|not)\s+(will\s+)?(require|need)\s+(any\s+)?sponsor", low):
                pref = [t for t in yes_opts
                        if not re.search(r"sponsor|h-?1b|visa", t.lower())]
            else:
                pref = [t for t in yes_opts
                        if re.search(r"sponsor|h-?1b|visa", t.lower())]
            return (pref or yes_opts)[0]
        return yes_opts[0]
    if low in ("no", "n") or re.match(r"no\b", low):
        no_opts = [t for t in texts if re.match(r"^no\b(?!ne)", t.lower())]
        return no_opts[0] if no_opts else None
    ans_tokens = _content_tokens(answer)
    if len(ans_tokens) == 1:
        # single-word answers ("Asian", "Male") — exact option match first,
        # then any option containing the token
        tok = next(iter(ans_tokens))
        for t in texts:
            t_tokens = _content_tokens(t)
            if t_tokens == {tok}:
                return t
        for t in texts:
            if tok in _content_tokens(t):
                return t
        return None
    if len(ans_tokens) < 2:
        return None
    best, best_score = None, 0.0
    for t in texts:
        t_tokens = _content_tokens(t)
        if not t_tokens:
            continue
        score = len(t_tokens & ans_tokens) / len(t_tokens)
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= 0.4 else None


def fill_screening_selects(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Fill <select> dropdowns whose question matches a stored answer.

    Only stored answers are used (no AI in deterministic mode); dropdowns with
    no matching stored answer are left untouched and logged.
    """
    filled: list[str] = []
    if not ctx.answers:
        return filled
    try:
        els = page.locator("select")
    except Exception:
        return filled
    for i in range(els.count()):
        el = els.nth(i)
        if not _visible(el):
            continue
        try:
            if el.input_value():
                continue  # already answered
        except Exception:
            pass
        # id-pinned fast path: a stored answer whose question equals this
        # field's id (or name) is authoritative — used for compliance/EEO
        # selects whose surrounding text is unreliable (e.g. Qualtrics'
        # compliance_veteran_status / compliance_race / compliance_gender,
        # whose question text is shadowed by the resume-upload widget).
        eid = None
        try:
            eid = el.get_attribute("id") or el.get_attribute("name")
        except Exception:
            eid = None
        id_handled = False
        if eid:
            for q, a in ctx.answers:
                if q.strip() == eid.strip():
                    id_handled = True
                    opt = _pick_option_value(el, a)
                    if opt:
                        try:
                            el.select_option(label=opt, timeout=4000)
                        except Exception as e:
                            log("field_error", "screening_select", f"{type(e).__name__}: {str(e)[:100]}")
                        else:
                            filled.append(f"select[{i}]")
                            log("filled", "screening_select",
                                f"ID={eid} → {opt[:60]} (id-pinned answer)")
                    else:
                        log("unanswered_question", "select",
                            f"ID={eid} — id-pinned answer {a[:40]!r} matches no option")
                    break
        if id_handled:
            continue
        qtext = _question_text_for(el)
        answer = _match_answer(qtext, ctx.answers)
        if not answer:
            continue
        opt = _pick_option_value(el, answer)
        if not opt:
            log("unanswered_question", "select",
                f"Q≈{qtext[:80]} — stored answer {answer[:40]!r} matches no option")
            continue
        try:
            el.select_option(label=opt, timeout=4000)
        except Exception as e:
            log("field_error", "screening_select", f"{type(e).__name__}: {str(e)[:100]}")
            continue
        filled.append(f"select[{i}]")
        log("filled", "screening_select",
            f"Q≈{qtext[:80]} | A≈{answer[:60]} → {opt[:60]}")
    return filled


def _tag_checkbox_groups(page) -> int:
    """Tag every checkbox/radio input with the id of the nearest ancestor
    holding 2+ boxes (its visual group). Returns the number of inputs tagged."""
    try:
        return page.evaluate("""() => {
            const boxes = [...document.querySelectorAll('input[type=checkbox], input[type=radio]')];
            let gid = 0;
            for (const b of boxes) {
                let key = 'solo-' + boxes.indexOf(b);
                let node = b.parentElement;
                for (let d = 0; d < 5 && node && node !== document.body; d++) {
                    const cnt = node.querySelectorAll('input[type=checkbox], input[type=radio]').length;
                    if (cnt >= 2) {
                        if (!node.dataset.jfGid) node.dataset.jfGid = 'g' + (++gid);
                        key = node.dataset.jfGid;
                        break;
                    }
                    node = node.parentElement;
                }
                b.dataset.jfGroup = key;
            }
            return boxes.length;
        }""") or 0
    except Exception:
        return 0


def _group_question_text(members) -> str:
    """Question text for a checkbox/radio group. A fieldset legend counts
    ONLY when it looks like a question (contains '?') — rjsf forms render a
    bare <legend> with the page title (e.g. 'Apply') which must not shadow
    the real question text."""
    try:
        leg = members[0].locator("xpath=ancestor::fieldset[1]//legend[1]").first
        if leg.count():
            t = (leg.inner_text() or "").strip()
            if t and "?" in t:
                return t
    except Exception:
        pass
    return _question_text_for(members[0])


def fill_screening_checkbox_groups(page, ctx: ApplicationContext, log: LogFn) -> list[str]:
    """Tick checkbox/radio options for groups whose question matches a stored
    answer. A stored 'Yes'/'No' maps to the option labeled that way; text
    answers tick every option whose label matches them (select-all-apply
    groups) or the best single option for radio groups. Groups with no stored
    match are left untouched — never fabricate a choice.
    """
    filled: list[str] = []
    if not ctx.answers:
        return filled
    n = _tag_checkbox_groups(page)
    if not n:
        return filled
    try:
        boxes = page.locator("input[type=checkbox], input[type=radio]")
    except Exception:
        return filled
    groups: dict[str, list] = {}
    for i in range(boxes.count()):
        el = boxes.nth(i)
        if not _visible(el):
            continue
        # Skip controls the consent pass already owns (GDPR/work-auth/EEO/referral).
        key = _element_key(el)
        if any(re.search(p, key, re.IGNORECASE) for p, _ in _CONSENT_PATTERNS):
            continue
        try:
            g = el.get_attribute("data-jf-group") or "solo"
        except Exception:
            continue
        groups.setdefault(g, []).append(el)
    for g, members in groups.items():
        if len(members) < 2:
            continue  # a lone checkbox is handled by the consent pass
        qtext = _group_question_text(members)
        answer = _match_answer(qtext, ctx.answers)
        if not answer:
            continue
        low = answer.lower().strip()
        is_radio = (members[0].get_attribute("type") or "").lower() == "radio"
        # Plain yes/no answer: pick the option labeled that way.
        if re.match(r"^(yes|no)\b", low) and len(_content_tokens(low)) <= 2:
            want = low.split()[0]
            for m in members:
                try:
                    lab = m.locator("xpath=ancestor::label[1]").first
                    lab_t = (lab.inner_text() or "").strip() if lab.count() else ""
                except Exception:
                    lab_t = ""
                if not lab_t:
                    try:
                        lab_t = (_element_key(m) or "").strip()
                    except Exception:
                        lab_t = ""
                if lab_t.lower().startswith(want) and not (
                        want == "no" and lab_t.lower().startswith("none")):
                    if want == "yes" and re.search(r"sponsor|h-?1b|\bvisa\b", low):
                        if not re.search(r"sponsor|h-?1b|visa", lab_t.lower()):
                            continue
                    try:
                        if not m.is_checked():
                            _tick_checkbox(m)
                        filled.append(f"checkbox_group[{g}]")
                        log("filled", "screening_checkbox",
                            f"Q≈{qtext[:80]} | A≈{answer[:50]} → {lab_t[:60]}")
                    except Exception as e:
                        log("field_error", "screening_checkbox", f"{type(e).__name__}"[:120])
                    if is_radio:
                        break
            continue
        # Text answer: tick every option whose label matches (radios: best only).
        ans_tokens = _content_tokens(low)
        if len(ans_tokens) < 2:
            continue
        # Negative answers ("I have not worked...", "No", "None...") need a much
        # stronger label match: "Press Ganey — Employee" must NOT tick just
        # because the answer mentions Press Ganey.
        negative = re.match(r"^(i (have )?not\b|no\b|none\b|never\b)", low)
        best_t, best_score, best_m = None, 0.0, None
        for m in members:
            try:
                lab = m.locator("xpath=ancestor::label[1]").first
                lab_t = (lab.inner_text() or "").strip() if lab.count() else ""
            except Exception:
                lab_t = ""
            if not lab_t:
                continue
            t_tokens = _content_tokens(lab_t)
            if not t_tokens:
                continue
            hit = t_tokens & ans_tokens
            # Require 2+ shared content tokens: a short label like
            # "Qualtrics — Employee" must not tick on the single shared word
            # "Qualtrics" when the answer says the person has NEVER worked there.
            if len(hit) < 2:
                continue
            score = len(hit) / len(t_tokens)
            label_neg = re.search(r"\b(not|none|never)\b", lab_t.lower())
            if negative:
                ok = (label_neg and score >= 0.5) or score >= 0.75
            else:
                ok = score >= 0.5
            if not ok:
                continue
            if is_radio:
                if score > best_score:
                    best_t, best_score, best_m = lab_t, score, m
                continue
            try:
                if not m.is_checked():
                    _tick_checkbox(m)
                filled.append(f"checkbox_group[{g}]")
                log("filled", "screening_checkbox",
                    f"Q≈{qtext[:80]} | A≈{answer[:50]} → {lab_t[:60]}")
            except Exception as e:
                log("field_error", "screening_checkbox", f"{type(e).__name__}"[:120])
        if is_radio and best_m is not None:
            try:
                if not best_m.is_checked():
                    _tick_checkbox(best_m)
                filled.append(f"checkbox_group[{g}]")
                log("filled", "screening_checkbox",
                    f"Q≈{qtext[:80]} | A≈{answer[:50]} → {best_t[:60]}")
            except Exception as e:
                log("field_error", "screening_checkbox", f"{type(e).__name__}"[:120])
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

    def fill(self, page, ctx: ApplicationContext, log: LogFn, drafter=None) -> list[str]:
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
                    if _already_filled(el):
                        log("skipped_field", field,
                            "already filled (resume autofill?) — kept")
                        continue
                    _fill_text(el, value)
                    filled.append(field)
                    log("filled", field, value[:120])
            except Exception as e:
                log("field_error", field, f"{type(e).__name__}"[:120])
        filled += fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log, drafter=drafter)
        filled += fill_screening_selects(page, ctx, log)
        filled += fill_screening_checkbox_groups(page, ctx, log)
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

    def fill(self, page, ctx: ApplicationContext, log: LogFn, drafter=None) -> list[str]:
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
                    if _already_filled(el):
                        log("skipped_field", field,
                            "already filled (resume autofill?) — kept")
                        continue
                    _fill_text(el, value)
                    filled.append(field)
                    log("filled", field, value[:120])
            except Exception as e:
                log("field_error", field, f"{type(e).__name__}"[:120])
        filled += fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log, drafter=drafter)
        filled += fill_screening_selects(page, ctx, log)
        filled += fill_screening_checkbox_groups(page, ctx, log)
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

    def fill(self, page, ctx: ApplicationContext, log: LogFn, drafter=None) -> list[str]:
        # Ashby forms are React-heavy and mostly custom-labeled: heuristic-first
        filled = fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log, drafter=drafter)
        filled += fill_screening_selects(page, ctx, log)
        filled += fill_screening_checkbox_groups(page, ctx, log)
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

    def fill(self, page, ctx: ApplicationContext, log: LogFn, drafter=None) -> list[str]:
        log("adapter_note", "workday", "experimental — Workday SPA; best-effort heuristic fill")
        filled = fill_heuristic(page, ctx, log)
        filled += fill_screening_questions(page, ctx, log, drafter=drafter)
        filled += fill_screening_selects(page, ctx, log)
        filled += fill_screening_checkbox_groups(page, ctx, log)
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
