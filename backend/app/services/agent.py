"""Auto-apply agent — Phase 5 orchestration.

The agent is a single background thread the user starts/stops from the Jobs
page toggle. While running it continuously walks the user's matched jobs
(best resume-match first) and, for each one that has not been handled yet:

1. **Reads the JD** (``services.tailor.fetch_jd``).
2. **Gates it** (``services.eligibility.assess_jd``): the user's rule is that a
   JD saying "US citizens only" or "no sponsorship" is a hard reject — the
   agent declines *before touching the form*. A JD that says nothing applies
   to these topics gets applied to.
3. **Applies** (``automation.runner.run_application`` with ``auto_submit=True``):
   fills the form, answers screening questions (stored answers first, then
   AI-drafted humanized answers), and clicks the employer's submit button.
   No human in the loop. CAPTCHA-gated forms are logged as blocked and skipped
   (hard rule: never solve a CAPTCHA) — the agent moves on.

The agent keeps a short in-memory event feed + counters for the dashboard, and
persists every decision as an ``Application`` row (status ``rejected_jd`` for
declines, so the tracker shows exactly what the agent did and why).

Pacing: one job at a time with a short pause between jobs. "Continuously" —
not literally one application per second — which is what keeps the employer's
ATS from rate-limiting or banning the browser.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from ..db.models import Application, Job

AGENT_INTERVAL_S = 15          # pause between jobs (each run itself takes 30-90s)
IDLE_POLL_S = 20               # re-check for new jobs when the queue is empty
MAX_ATTEMPTS = 2               # retry a failed run at most this many times
MAX_EVENTS = 60

_HANDLED_STATUSES = {
    "submitted", "submit_uncertain", "submit_blocked_captcha", "rejected_jd",
    "rejected_clearance", "dry_run_completed", "ready_for_submission",
    "captcha_manual", "manual_required", "submit_failed", "failed",
}


class AutoApplyAgent:
    """One instance per app, attached at ``app.state.agent``."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.session_factory = None      # set by create_app
        self.min_score: int = 0          # skip jobs below this match score (0 = all matched)
        self.last_activity: str | None = None
        self.last_error: str | None = None
        self.stats = {"submitted": 0, "review": 0, "blocked": 0,
                      "rejected": 0, "failed": 0}
        self.events: list[dict] = []

    # ------------------------------------------------------------- control

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, min_score: int = 0) -> bool:
        with self._lock:
            if self.is_running:
                return False
            self.min_score = max(0, int(min_score or 0))
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="auto-apply-agent", daemon=True)
            self._thread.start()
            self._event("agent", None, "agent started")
            return True

    def stop(self) -> bool:
        with self._lock:
            if self._thread is None:
                return False
            self._stop.set()
            self._event("agent", None, "agent stopped")
            return True

    def wait(self, timeout: float = 60.0) -> None:
        """Join the worker thread (tests / clean shutdown)."""
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def status(self) -> dict:
        return {
            "running": self.is_running,
            "stats": dict(self.stats),
            "last_activity": self.last_activity,
            "last_error": self.last_error,
            "events": list(self.events[-12:][::-1]),
        }

    # --------------------------------------------------------------- loop

    def _loop(self) -> None:
        from sqlalchemy.orm import Session

        while not self._stop.is_set():
            db: Session = self.session_factory()
            try:
                job = self._next_candidate(db)
                if job is None:
                    self.last_activity = "no new jobs — waiting"
                    self._stop.wait(IDLE_POLL_S)
                    continue
                self._process_job(db, job)
            except Exception as e:  # agent must never die silently
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                self._event("agent", None, f"error: {self.last_error}")
            finally:
                db.close()
            self._stop.wait(AGENT_INTERVAL_S)

    def _next_candidate(self, db) -> Job | None:
        """Best-matched job that has an apply URL and is not yet handled."""
        jobs = (
            db.query(Job)
            .filter(Job.passes_filters.is_(True), Job.apply_url.isnot(None))
            .all()
        )
        # Order: highest match first (None last), newest first as tiebreak.
        jobs.sort(key=lambda j: (-(j.match_score if j.match_score is not None else -1),
                                 -j.id))
        for job in jobs:
            if self.min_score > 0 and (
                job.match_score is None or job.match_score < self.min_score
            ):
                continue
            attempts = (
                db.query(Application)
                .filter(Application.job_id == job.id)
                .all()
            )
            statuses = [a.status for a in attempts]
            if any(s in _HANDLED_STATUSES for s in statuses):
                continue
            if statuses.count("failed") >= MAX_ATTEMPTS:
                continue
            return job
        return None

    def _process_job(self, db, job: Job) -> None:
        """Gate + apply one job. Persists an Application row either way."""
        from ..automation.runner import run_application
        from ..services.eligibility import assess_jd
        from ..services.tailor import fetch_jd

        title = f"{job.title} @ {job.company}"
        self._event("jd", job, f"reading JD: {title}")
        try:
            jd_text = fetch_jd(job)
        except Exception as e:
            jd_text = ""
            self.last_error = f"JD fetch failed for {job.id}: {type(e).__name__}"

        assessment = assess_jd(jd_text)
        job.jd_eligibility_json = json.dumps(assessment)

        from ..db.models import Profile
        from ..automation.runner import _resume_for_job
        try:
            resume = _resume_for_job(db, job, None)
            resume_id = resume.id
        except ValueError:
            resume_id = None

        if assessment["decision"] == "reject":
            if resume_id is None:
                # Nothing to attach the decline to — count + log, move on.
                self.stats["rejected"] += 1
                self._event("rejected", job,
                            "JD gate rejected (no resume on file — nothing recorded)")
                return
            app_row = Application(
                job_id=job.id,
                resume_id=resume_id, dry_run=False,
                status="rejected_jd",
                finished_at=datetime.utcnow(),
                error_text=assessment["note"],
                notes_json=json.dumps(assessment)[:8000],
            )
            db.add(app_row)
            db.commit()
            self.stats["rejected"] += 1
            reason = "; ".join(assessment["reasons"])
            self._event("rejected", job, f"JD gate rejected: {reason}")
            self.last_activity = f"rejected {title} — {reason[:120]}"
            return

        self._event("applying", job,
                    f"applying (auto-submit) — {assessment['note']}")

        # User's vision: tailor the resume to THIS job's JD before filling.
        # Deterministic (truth-only) when no LLM key is configured.
        if not job.tailored_resume_id:
            try:
                from ..services.tailor import tailor_job
                tailor_job(db, job.id, jd_text=jd_text, cover_letter=True)
                db.commit()
                db.refresh(job)
                self._event("tailored", job, "resume tailored to this JD before applying")
            except Exception as e:
                self._event("tailor_failed", job,
                            f"tailoring failed ({type(e).__name__}) — applying with master resume")

        result = run_application(db, job.id, dry_run=False,
                                 headless=True, auto_submit=True)
        status = result.get("status", "failed")
        self._tally(status)
        self.last_activity = f"{status}: {title}"
        self._notify_result(db, job, status, result)

    def _notify_result(self, db, job: Job, status: str, result: dict) -> None:
        """User rule: when the agent can't finish on its own (CAPTCHA above
        all), raise a flag and notify the user IMMEDIATELY for manual action."""
        from ..services.notify import notify

        title = f"{job.title} @ {job.company}"
        if status in ("captcha_manual", "submit_blocked_captcha"):
            where = ("the form was filled but the SUBMIT button is behind a CAPTCHA"
                     if status == "submit_blocked_captcha"
                     else "the form is behind a CAPTCHA — it was not filled")
            notify(db, "action",
                   f"CAPTCHA needs you: {job.company}",
                   f"{title} — {where}. Open the application link and finish it manually.",
                   link=job.apply_url)
        elif status == "submitted":
            notify(db, "info",
                   f"Applied: {job.company}",
                   f"{title} — the agent tailored your resume, filled the form and submitted it.",
                   link=result.get("detail_url"))
        elif status == "rejected_clearance":
            notify(db, "info",
                   f"Skipped (security clearance): {job.company}",
                   f"{title} — the posting/form requires a security clearance. "
                   "Auto-skipped per your rule (not authorized for clearance "
                   "work). Nothing was filled or submitted.",
                   link=job.apply_url)
        elif status in ("submit_failed", "manual_required", "failed"):
            notify(db, "action",
                   f"Agent stuck: {job.company}",
                   f"{title} — {status}: {(result.get('error') or status)[:200]} "
                   "Please review and apply manually if this job matters.",
                   link=job.apply_url)

    def _tally(self, status: str) -> None:
        if status == "submitted":
            key = "submitted"
        elif status in ("submit_blocked_captcha", "captcha_manual", "submit_uncertain"):
            key = "blocked"
        elif status == "rejected_jd":
            key = "rejected"
        elif status == "failed":
            key = "failed"
        else:
            key = "review"
        self.stats[key] = self.stats.get(key, 0) + 1

    def _event(self, kind: str, job: Job | None, text: str) -> None:
        ev = {
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "kind": kind,
            "job": f"{job.title} @ {job.company}" if job is not None else None,
            "text": text[:200],
        }
        self.events.append(ev)
        del self.events[:-MAX_EVENTS]
        self.last_activity = ev["text"]


def get_agent(request) -> AutoApplyAgent:
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        agent = AutoApplyAgent()
        agent.session_factory = request.app.state.SessionLocal
        request.app.state.agent = agent
    return agent
