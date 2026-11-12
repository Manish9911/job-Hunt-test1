# External repo study — accuracy & precision takeaways (2026-11-12)

Studied 4 repos to find techniques that make jobflow's matching, pre-screening, and
application automation more accurate. Clones: `~/.cache/scratch/repos/` (scratch).

## What each repo is

| Repo | Reality | Use to us |
|---|---|---|
| `Francis1998/agentic-career-search` | Actually **PARK0301/job-agent** — a personal Python job-hunt agent. Closest architecture twin to jobflow: 0-100 scoring, deterministic pre-screen, CDP form fill, SQLite tracking. 0 stars, 2 commits — design reference, not a dependency. | HIGH — matching + reliability patterns |
| `rainmanjam/jobspy-api` | FastAPI microservice wrapping the `python-jobspy` library (7 boards). Pure discovery/transport: no matching, no persistence, no dedupe. Real value is its **API-boundary discipline** (per-board param-conflict validation, canonical enums, TTL cache). | MEDIUM — discovery/ingest hygiene |
| `ScrapingBee/google-jobs-api` | **Docs-only marketing repo** for a paid managed scraper. Only README. | NONE (paywalled API; we already scrape Google-for-Jobs-style data via hiring.cafe) |
| `fedorchenko-juli/job_search_tool` | Clean 900-line pipeline: direct ATS-API fetch (Greenhouse/Ashby/Workable/Lever/SmartRecruiters) + aggregators, allowlist-first title filters, dedupe by URL **and** normalized company+title, optional Claude functional-fit scoring, local review board. | HIGH — dedupe + filter + LLM-eval engineering details |

## Gaps in jobflow today (verified in code)

1. **Dedupe key** (`ingestion/service.py:46`): `sha1(source | apply_url or company|title)` — no normalization. A repost under a new URL, or "Senior Java Developer" vs "Senior Java Developer (Remote)" from another source, double-stores.
2. **No failure taxonomy**: submit outcomes are free-form status strings; we can't answer "which ATS wall eats most attempts" without manual counting.
3. **Pre-screen regexes run on raw description text** (HTML entities, mid-sentence truncation from aggregator snippets) — false-negative risk on visa/clearance phrasing.
4. **No feedback loop into scoring**: applications DB has outcomes but scoring never learns from them.
5. **LLM scoring designed but dormant** (no API key): the deterministic scorer is the only scorer; Phase 3 LLM path has no prompt/schema/retry engineering yet.

## Ranked improvements (value / effort)

### P1 — Normalized dedupe key (small, immediate precision win)
From job_search_tool `app/dedup.py:85`:
```
key = slug(company) + slug(title) + slug(location)   # slug = lowercase, alnum-only
```
plus keep URL as secondary key. Adopt: store BOTH `unique_key` (existing, stable) and a new
`norm_key = sha1(source-agnostic slug(company)|slug(title)|slug(location_state))`; skip on either hit.
This catches cross-board duplicates (hiring.cafe + ATS feeds are the same postings).

### P2 — Truncation-aware scoring & pre-screen (medium, precision win)
From job_search_tool `app/ai_evaluate.py` (`looks_truncated` + partial-note in prompt) and
jobspy-api (canonical markdown descriptions):
- Detect short/snippet descriptions (`len < N` or no sentence-ending period in last 40 chars).
- Flag `jd_truncated=True`; when truncated, experience/stack rules should **not hard-reject** on
  missing requirements — demote to `review`, and tag the score breakdown.
- Store canonical markdown description alongside raw HTML; run all regexes on the canonical form.

### P3 — Failure taxonomy on applications (small, reliability win)
From job-agent `src/apply_telemetry.py`: classify every non-`submitted` outcome into
`login_gate | captcha | resume_upload | empty_required | timeout | unsupported | wall_bot | stale_url`.
Add a `failure_class` column (nullable) + weekly report endpoint
(`submitted / total` per class per ATS host). Directly tells us where the 150+ `submit_uncertain`
headless failures went.

### P4 — LLM eval engineering (when API key lands)
From job_search_tool `app/ai_evaluate.py` — all three lessons are production-hardened:
- **Tool-call schema, not free JSON**: `submit_evaluation` with `match_score` + `recommendation`
  (apply/consider/skip) declared FIRST (short fields survive truncation), then gaps/strengths/risks.
- **Retry once with +512 max_tokens** on missing fields instead of crashing the run.
- **Honesty prompt**: "Be honest and specific, not diplomatic... name the gap precisely."
From job-agent `prompts/match.txt`:
- LLM score is a **ranking nudge only**; only hard deterministic filters reject.
- Unclear visa → down-weight, never auto-reject.
Both feed our existing `score_job()` signature — deterministic stays the rejection gate.

### P5 — Feedback-adjusted scoring (medium, compounding)
From job-agent `src/feedback_signals.py` (each adjustment clamped + reason-coded):
- company skipped >=2x → -5 each (cap 15)
- channel submit-rate >=0.70 with >=3 attempts → +5 (that ATS is automatable for us)
- company with interview/offer outcome → +8 each (cap 16)
Wire to `score_all_jobs()` after outcomes are recorded; store `score_adjustments_json` for audit.

### P6 — ATS regression fixtures (medium, long-term)
From job-agent `tests/fixtures/ats/*.html`: save captured form HTML per ATS (Ashby, Greenhouse,
Workday, Paylocity, Dover, PageUp...) and run fill strategies against them in pytest. We currently
regress only against live sites — this makes form-fill precision testable offline.

### P7 — Per-board parameter-conflict validation (small)
From jobspy-api `app/routes/api.py`: a table of mutually-exclusive filters per board, validated
before the request, with actionable errors. Prevents "board silently ignored the filter →
imprecise candidate set" bugs in our ingest endpoints.

### Skip
- ScrapingBee google-jobs-api: paid, closed, nothing portable.
- job-agent's LinkedIn Easy-Apply CDP path: ToS-risky, not our channel.
- jobspy-api's filter bugs (`.str.contains` without `regex=False`, `astype(float)` without NaN
  guard) — copy the shape, not the lines.

## Truth-contract note
None of the repos do the resume-tailing truth contract (reorder/reword/emphasize only, never
invent) — jobflow's is stronger. Keep it. job-agent's "learning inbox" (human approves learned
answers before they become canonical) is a good model for how CommonAnswers learnings should
land: propose, don't auto-write.
