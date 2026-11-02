"""Phase 4 — ATS application automation (Playwright).

Hard rules (from the project spec) enforced in this package:
  * DRY-RUN is the default: everything is filled and screenshotted, but the
    form is NEVER submitted automatically. Phase 4 stops at
    "dry_run_completed" / "ready_for_submission"; a human submits.
  * Never solve CAPTCHAs: detection raises/aborts with status
    "captcha_manual" so a human can finish the step.
  * No passwords are stored anywhere; only public profile data is used.
  * Every field touched is written to the action_log audit trail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


class CaptchaDetected(Exception):
    """A CAPTCHA / bot-challenge was found. Hard rule: never solve — a human must."""

    def __init__(self, evidence: str):
        self.evidence = evidence
        super().__init__(f"CAPTCHA detected: {evidence}")


@dataclass
class ApplicationContext:
    """Everything an adapter may fill, sourced ONLY from the profile + resume.

    No free-text generation happens here — every value is a literal from the
    profile/resume DB (the truth contract carries over to applications).
    """

    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    _Text = str  # type alias (keeps annotation tooling simple)
    work_authorization: _Text = ""   # e.g. "US - H-1B sponsorship needed"
    sponsorship_needed: bool = True
    region: str = "us"              # us | eu  (drives EEO vs GDPR handling)
    linkedin: str = ""
    portfolio: str = ""
    resume_path: str = ""
    resume_filename: str = ""
    # (question, answer) pairs for screening questions
    answers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_profile(cls, profile, resume=None) -> "ApplicationContext":
        name = (profile.full_name or "").strip()
        parts = name.split(None, 1)
        ctx = cls(
            first_name=parts[0] if parts else "",
            last_name=" ".join(parts[1:]) if len(parts) > 1 else "",
            full_name=name,
            email=profile.email or "",
            phone=profile.phone or "",
            location=profile.location or "",
            work_authorization=profile.work_authorization or "",
            sponsorship_needed=bool(getattr(profile, "sponsorship_needed", True)),
        )
        if resume is not None:
            ctx.resume_path = resume.storage_path or ""
            ctx.resume_filename = resume.original_filename or "resume.docx"
        try:
            import json

            locs = json.loads(getattr(profile, "locations_json") or "[]")
            if locs and isinstance(locs, list):
                first = str(locs[0]).lower()
                if first.startswith("eu") or first == "europe":
                    ctx.region = "eu"
                elif first.startswith("us"):
                    ctx.region = "us"
        except (ValueError, TypeError):
            pass
        return ctx


LogFn = Callable[[str, str | None, str | None], None]
"""log(action, field, detail) — provided by the runner, writes ActionLog rows."""


class AtsAdapter:
    """Strategy for one ATS family. `fill` returns the labels of fields it filled."""

    name = "generic"

    @classmethod
    def detect(cls, url: str) -> bool:
        return False

    def fill(self, page, ctx: ApplicationContext, log: LogFn, drafter=None) -> list[str]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# CAPTCHA detection (never solve — detect and hand off to a human)
# ---------------------------------------------------------------------------

_CAPTCHA_PATTERNS = [
    (r"g-recaptcha|google\.com/recaptcha|recaptcha/api", "reCAPTCHA"),
    (r"h-captcha|hcaptcha\.com", "hCaptcha"),
    (r"cf-turnstile|challenges\.cloudflare\.com|turnstile", "Cloudflare Turnstile"),
    (r"challenge-platform|just a moment", "Cloudflare challenge"),
    (r"are you a robot|verify you are human|confirm you're human", "human-verification prompt"),
]


def check_captcha(page) -> None:
    """Raise CaptchaDetected if any known bot-challenge marker is on the page."""
    try:
        html = (page.content() or "").lower()
    except Exception:
        return
    for pattern, label in _CAPTCHA_PATTERNS:
        if re.search(pattern, html, re.IGNORECASE):
            raise CaptchaDetected(label)
