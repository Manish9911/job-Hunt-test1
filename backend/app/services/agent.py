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
INGEST_ROTATE_MIN_GAP_S = 30 * 60  # rotate sources (pull fresh jobs) at most every 30 min

_HANDLED_STATUSES = {
    "submitted", "submit_uncertain", "submit_blocked_captcha", "rejected_jd",
    "rejected_clearance", "dry_run_completed", "ready_for_submission",
    "captcha_manual", "manual_required", "submit_failed", "failed",
    # Permission model (2026-11-09): fraud-screen outcomes, user rejections,
    # and the never-pays-a-fee abort.
    "rejected_fraud", "flagged_fraud_review", "rejected_user",
    "submit_blocked_payment",
    # Self-improvement loop (2026-11-09): a CAPTCHA-walled board is skipped,
    # not re-battled, until the user clears the wall.
    "skipped_walled",
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
        # Self-improvement habit (2026-11-09): handoff file + source rotation
        self.data_dir = None                    # set by create_app
        self._last_ingest_ts: float = 0.0
        self._walled_notified: set[str] = set() # domains notified this run

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
            # Handoff habit: if a previous run left a status snapshot, say so.
            try:
                from ..services.learn import read_status_file
                if self.data_dir is not None:
                    handoff = read_status_file(self.data_dir)
                    if handoff.get("running_at_end"):
                        self._event("agent", None,
                                    f"resuming handoff — last activity: "
                                    f"{str(handoff.get('last_activity'))[:100]}")
            except Exception:
                pass
            return True

    def stop(self) -> bool:
        with self._lock:
            if self._thread is None:
                return False
            self._stop.set()
            self._event("agent", None, "agent stopped")
            try:
                if self.data_dir is not None:
                    from ..services.learn import read_status_file, write_status_file
                    payload = read_status_file(self.data_dir)
                    payload["running_at_end"] = False
                    payload["stopped_at"] = datetime.utcnow().isoformat()
                    write_status_file(self.data_dir, payload)
            except Exception:
                pass
            return True

    def wait(self, timeout: float = 60.0) -> None:
        """Join the worker thread (tests / clean shutdown)."""
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def status(self) -> dict:
        lessons = 0
        try:
            if self.session_factory is not None:
                from ..db.models import Lesson
                db = self.session_factory()
                try:
                    lessons = db.query(Lesson).count()
                finally:
                    db.close()
        except Exception:
            pass
        return {
            "running": self.is_running,
            "stats": dict(self.stats),
            "last_activity": self.last_activity,
            "last_error": self.last_error,
            "lessons": lessons,
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
                    # Rotation habit (2026-11-09): instead of idling on an
                    # exhausted queue, pull fresh jobs from the sources —
                    # "switch to a different source, don't re-battle the wall".
                    now = time.time()
                    if now - self._last_ingest_ts >= INGEST_ROTATE_MIN_GAP_S:
                        self._last_ingest_ts = now
                        self._event("rotate", None,
                                    "queue exhausted — rotating sources, pulling fresh jobs")
                        try:
                            from ..ingestion.service import ingest_company_feed_jobs
                            res = ingest_company_feed_jobs(db, limit=50)
                            self._event("rotate", None,
                                        f"rotation done: {res}")
                        except Exception as e:
                            self._event("rotate", None,
                                        f"rotation failed: {type(e).__name__}: {str(e)[:120]}")
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
            # Judge by the LATEST row, not any row: a job whose newest row is
            # 'pending' (e.g. a false-positive rejection the user or we
            # requeued) must be retried — the old "any handled row => skip"
            # check made requeued jobs unrunnable forever (live incident
            # 2026-11-11: 21 requeued rows sat untouched while the agent
            # idled on "no new jobs").
            if not attempts:
                return job
            latest = max(attempts, key=lambda a: a.id)
            if latest.status in _HANDLED_STATUSES:
                continue
            if [a.status for a in attempts].count("failed") >= MAX_ATTEMPTS:
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

        # Fraud / legitimacy screen (2026-11-09, job-apply-agent safety
        # model): BEFORE the JD gate and before any browser work. A fee or
        # identity-document demand is a hard reject; uncertain legitimacy is
        # held for the user — no personal data goes to a suspect employer.
        from ..services import learn
        from ..services.fraud import assess_job_fraud
        fraud = assess_job_fraud(jd_text, job.apply_url,
                                 getattr(job, "company_website", None))
        # Learn from earlier calls: once the user decided this domain/company,
        # the same call is never re-asked (the "ask once, save, reuse" habit).
        verdict = learn.domain_verdict(db, job)
        if verdict == "scam":
            fraud = {"decision": "reject",
                     "reasons": ["user previously confirmed this is a scam (saved lesson)"],
                     "note": "Hard-rejected from a saved lesson — the user "
                             "already confirmed this as a scam. Nothing filled "
                             "or submitted."}
            self._event("learn", job, "saved 'scam' lesson — hard reject, no re-ask")
        elif verdict == "ok" and fraud["decision"] == "review":
            fraud = {**fraud, "decision": "apply", "reasons": [],
                     "note": "fraud screen flagged it, but the user previously "
                             "confirmed this employer is legit — proceeding"}
            self._event("learn", job, "saved 'legit' lesson — not re-flagged")
        job.fraud_screen_json = json.dumps(fraud)
        if fraud["decision"] in ("reject", "review"):
            from ..db.models import Profile
            from ..automation.runner import _resume_for_job
            try:
                resume = _resume_for_job(db, job, None)
                resume_id = resume.id
            except ValueError:
                resume_id = None
            status = "rejected_fraud" if fraud["decision"] == "reject" else "flagged_fraud_review"
            if resume_id is not None:
                app_row = Application(
                    job_id=job.id,
                    resume_id=resume_id, dry_run=False,
                    status=status,
                    finished_at=datetime.utcnow(),
                    error_text=fraud["note"],
                    notes_json=json.dumps(fraud)[:8000],
                )
                db.add(app_row)
                db.commit()
            reason = "; ".join(fraud["reasons"])
            self.stats["rejected"] += 1 if fraud["decision"] == "reject" else 0
            self.stats["review"] += 1 if fraud["decision"] == "review" else 0
            self._event("fraud", job, f"fraud screen: {fraud['decision']} — {reason[:150]}")
            self.last_activity = f"fraud screen {fraud['decision']}: {title}"
            self._notify_fraud(db, job, fraud)
            return

        assessment = assess_jd(jd_text)

        from ..db.models import Profile
        from ..automation.runner import _resume_for_job
        try:
            resume = _resume_for_job(db, job, None)
            resume_id = resume.id
        except ValueError:
            resume_id = None

        # Cheap parameter pre-screen, continued: judge the JD's actual stack,
        # not just the title (user rule 2026-11-05 — companies name roles
        # differently). Deterministic + token-free: reject only when the JD is
        # built around 3+ canonical skills the resume has NONE of.
        if assessment["decision"] == "apply":
            from ..ingestion.service import load_resume_skills
            from ..services.eligibility import assess_jd_skills
            stack = assess_jd_skills(jd_text, load_resume_skills(db))
            if stack["decision"] == "reject":
                assessment["decision"] = "reject"
                assessment["reasons"].extend(stack["reasons"])
                assessment["note"] = ("JD stack mismatch — application refused "
                                      "before filling anything")

        job.jd_eligibility_json = json.dumps(assessment)

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

        # Wall habit (2026-11-09): if this board has walled us (3+ consecutive
        # CAPTCHAs), do NOT burn another full browser run on it — park the job
        # for the user, notify once per domain, and move on.
        wall = learn.board_is_walled(db, job)
        if wall is not None:
            from ..automation.runner import _resume_for_job
            try:
                resume = _resume_for_job(db, job, None)
                resume_id = resume.id
            except ValueError:
                resume_id = None
            if resume_id is not None:
                app_row = Application(
                    job_id=job.id, resume_id=resume_id, dry_run=False,
                    status="skipped_walled", finished_at=datetime.utcnow(),
                    error_text=f"board CAPTCHA-walled ({wall.text[:200]}) — "
                               "skipped; clear the wall and it will retry",
                )
                db.add(app_row)
                db.commit()
            from ..services.learn import _domain as _lnd
            dom = _lnd(job.apply_url)
            if dom not in self._walled_notified:
                self._walled_notified.add(dom)
                from ..services.notify import notify
                notify(db, "action",
                       f"CAPTCHA-walled board: {job.company}",
                       f"{title} — this board has CAPTCHA-walled the agent "
                       f"{wall.count} times in a row, so it is being skipped "
                       "instead of re-battled. Clear the wall once (or apply "
                       "manually), then press 'wall cleared' on the "
                       "Applications page.",
                       link=job.apply_url)
            self.stats["blocked"] = self.stats.get("blocked", 0) + 1
            self._event("walled", job, f"skipped — {wall.text[:120]}")
            self.last_activity = f"skipped (walled board): {title}"
            self._write_handoff(db)
            return

        # Permission model (2026-11-09): 'approve_each' stages the application
        # (form filled + screenshotted, NOTHING submitted) and waits for the
        # user to approve or reject it from the Applications page.
        from ..automation.runner import _find_profile
        prof = _find_profile(db)
        approval_mode = bool(prof) and prof.submission_mode == "approve_each"

        if approval_mode:
            self._event("staging", job,
                        f"filling form (approval mode — you decide the submit)")
        else:
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

        if approval_mode:
            result = run_application(db, job.id, dry_run=True,
                                     headless=True, auto_submit=False)
            status = result.get("status", "failed")
            self._tally(status)
            self.last_activity = f"staged (awaiting your approval): {title}"
            self._notify_result(db, job, status, result, approval_mode=True)
            learn.record_outcome(db, job, status)
            self._write_handoff(db)
            return

        result = run_application(db, job.id, dry_run=False,
                                 headless=True, auto_submit=True)
        status = result.get("status", "failed")
        self._tally(status)
        self.last_activity = f"{status}: {title}"
        self._notify_result(db, job, status, result)
        learn.record_outcome(db, job, status)
        self._write_handoff(db)

    def _write_handoff(self, db) -> None:
        """campaign_status.md habit: a restart-friendly snapshot of where the
        run stands, written after every job so a killed/restarted agent can
        resume context instead of guessing."""
        try:
            if self.data_dir is None:
                return
            from ..db.models import Application
            from ..services.learn import read_status_file, write_status_file

            pending = db.query(Application).count()
            walled = []
            try:
                from ..db.models import Lesson
                for l in db.query(Lesson).filter(Lesson.kind == "wall").all():
                    walled.append(l.scope)
            except Exception:
                pass
            payload = {
                "running_at_end": True,
                "min_score": self.min_score,
                "stats": dict(self.stats),
                "last_activity": self.last_activity,
                "last_error": self.last_error,
                "applications_total": pending,
                "walled_boards": walled,
                "updated_at": datetime.utcnow().isoformat(),
            }
            write_status_file(self.data_dir, payload)
        except Exception:
            pass

    def _notify_fraud(self, db, job: Job, fraud: dict) -> None:
        from ..services.notify import notify

        title = f"{job.title} @ {job.company}"
        reason = "; ".join(fraud["reasons"])[:300]
        if fraud["decision"] == "reject":
            notify(db, "info",
                   f"Skipped (fraud screen): {job.company}",
                   f"{title} — {reason} Dropped per the never-pay-a-fee rule; "
                   "nothing was filled or submitted.",
                   link=job.apply_url)
        else:
            notify(db, "action",
                   f"Check this posting: {job.company}",
                   f"{title} — legitimacy uncertain ({reason}). The agent did "
                   "NOT apply. If it's a real employer, apply manually or "
                   "tell me to re-run it.",
                   link=job.apply_url)

    def _notify_result(self, db, job: Job, status: str, result: dict,
                       approval_mode: bool = False) -> None:
        """User rule: when the agent can't finish on its own (CAPTCHA above
        all), raise a flag and notify the user IMMEDIATELY for manual action."""
        from ..services.notify import notify

        title = f"{job.title} @ {job.company}"
        if approval_mode and status in ("dry_run_completed", "ready_for_submission"):
            notify(db, "action",
                   f"Your approval needed: {job.company}",
                   f"{title} — the agent tailored your resume and filled the "
                   "form (approval mode). Nothing was submitted. Approve or "
                   "reject it on the Applications page.",
                   link=job.apply_url)
            return
        if status == "submit_blocked_payment":
            notify(db, "action",
                   f"Blocked (payment demand): {job.company}",
                   f"{title} — the application form asks for payment/card "
                   "details. The agent aborted before filling anything "
                   "(hard rule: never pay a fee). Do NOT complete this "
                   "application manually unless you know why a fee is asked.",
                   link=job.apply_url)
            return
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
        elif status in ("submit_blocked_captcha", "captcha_manual",
                        "submit_uncertain", "submit_blocked_payment"):
            key = "blocked"
        elif status in ("rejected_jd", "rejected_fraud", "rejected_user"):
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
