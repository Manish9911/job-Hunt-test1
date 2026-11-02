# jobflow

Local, human-in-the-loop automated job-application pipeline.

> **Status: Phase 2 complete (job ingestion) — on branch `phase-2-ingestion`, pending approval.**

## What it will do

1. **Dashboard** — manage profile, master resume + resume versions, skills, work history, education, work-authorization answers, and a common-application-answers store (Phase 1).
2. **Job ingestion** — parse the [hiring-without-whiteboards](https://github.com/poteto/hiring-without-whiteboards) company list, discover each company's careers page/ATS, pull listings from [hiring.cafe](https://hiring.cafe), filter by preferences, deduplicate (Phase 2).
3. **LLM tailoring** — score a job vs. the master resume and produce a tailored DOCX/PDF + optional cover letter. Never invents experience, employers, dates, degrees, or skills — reorder/reword/emphasize only, with a visible diff (Phase 3).
4. **Application automation** — Playwright adapters (Greenhouse, Lever, Ashby, Workday first) auto-fill from the profile, upload the tailored resume, answer screening questions from the stored answers. **DRY-RUN by default**: fill + screenshot + queue for human review (Phase 4).
5. **Orchestration & tracking** — discover → score → tailor → fill → review → submit pipeline, application tracker with status/logs/screenshots, rate limiting, retries, daily summary (Phase 5).

## Hard rules

- Human approval before every final submission by default. Auto-submit only when explicitly enabled and only above a configured match score.
- No CAPTCHA/bot-detection solving — pause and ask.
- Respect robots.txt and site ToS; polite rate limits; skip sites that prohibit automation.
- Unanswerable questions (salary, sponsorship, essays) pause the pipeline and ask — never guess.
- No passwords stored; login/session handling is manual or cookie-based.
- Every action is logged for audit.

## Architecture (Phase 0 proposal)

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐
│  Frontend   │────▶│  FastAPI backend │────▶│   SQLite          │
│  HTMX/      │◀────│  (REST + pages)  │◀────│   (SQLAlchemy)    │
│  Jinja2     │     └──────┬───────────┘     └───────────────────┘
└─────────────┘            │
        ┌──────────────────┼───────────────────────┐
        ▼                  ▼                       ▼
┌───────────────┐  ┌──────────────────┐  ┌────────────────────┐
│  Ingestion    │  │  LLM tailoring   │  │  Playwright        │
│  hwow parser, │  │  match score,    │  │  ATS adapters:     │
│  hiring.cafe, │  │  tailored DOCX/  │  │  Greenhouse, Lever,│
│  ATS discovery│  │  PDF, diff,      │  │  Ashby, Workday    │
└───────────────┘  │  cover letter    │  │  (dry-run default) │
                   └──────────────────┘  └────────────────────┘
```

**Folder structure**

```
jobflow/
├── backend/app/
│   ├── main.py            # FastAPI entrypoint
│   ├── api/               # routers: profiles, resumes, jobs, applications, pipeline
│   ├── db/                # SQLAlchemy models, session, migrations
│   ├── services/          # resume parser, pipeline orchestration
│   ├── llm/               # swappable provider interface + openai/anthropic/ollama impls
│   ├── ingestion/         # hiring-without-whiteboards parser, hiring.cafe, ATS discovery
│   └── automation/        # Playwright base + per-ATS adapters, dry-run runner
├── frontend/
│   ├── templates/         # HTMX + Jinja2 dashboard pages
│   └── static/
├── tests/                 # pytest (per-phase tests)
├── data/                  # gitignored: sqlite db, resumes, screenshots, cookies
├── .env.example
└── pyproject.toml
```

**Data model (SQLite, Phase 1)**

| Table | Purpose |
|---|---|
| `profiles` | one user profile: contact, location, work authorization, summary |
| `resumes` | resume files + parsed structured JSON; one flagged `is_master` |
| `skills` | categorized skill list |
| `work_history` | employers, titles, dates, bullet points (JSON) |
| `education` | degrees, schools, dates |
| `common_answers` | reusable screening answers (keyed by question/category) |
| `companies` | name, website, careers URL, detected ATS, region, source |
| `jobs` | listings with unique key for dedup, source, fetched_at, filters |
| `applications` | job + tailored resume + match score + status + dry_run flag |
| `action_log` | timestamped audit trail of every pipeline action |

**Tech choices**: Python 3.11+, FastAPI, SQLAlchemy 2 + SQLite (swap-ready to Postgres), Pydantic v2, HTMX + Jinja2 (simpler than a React SPA for a local single-user tool), Playwright, httpx, python-docx + pdfplumber, LLM behind a `complete(prompt, system)` protocol selected via `LLM_PROVIDER`. `uv` for env/deps.

**Key risks**

- **hiring.cafe anti-bot / layout changes** → polite rate limits, graceful degradation, manual URL paste fallback.
- **ATS diversity** → adapters are per-ATS and grow iteratively; dry-run-first keeps risk bounded.
- **LLM hallucination** → strict "truth-only" prompt contract + diff review gate; the tailoring step is always human-reviewed.
- **EU vs US screening questions** (GDPR, EEO, sponsorship) → common_answers store + pause-and-ask rule.
- **Legal/ToS** → robots.txt check, skip automation-prohibiting sites, no CAPTCHA solving.

## Confirmed preferences (Phase 0, 2026-11-01)

| Preference | Value |
|---|---|
| Target roles | Any SWE role matching the tech stack (Java, Spring Boot, React, PostgreSQL, REST APIs, microservices, AWS); minimum **4+ years** of experience |
| Locations | **US: remote + onsite**; **Europe: remote only** |
| Work authorization | **US work visa (H1B/L1)** — US employers may need to sponsor. EU: remote-from-abroad roles only |
| LLM provider | **OpenAI** (`gpt-4o-mini` default, `LLM_MODEL` overridable in `.env`) |
| GitHub | Repo `jobflow`, **private**, account `Manish9911` (created on user instruction) |
| Salary range | Not set yet — captured in the Phase 1 profile |

Master resume: `Manish_Badugu_AWS_SDE_II` (Java/Spring/React full stack, 4+ yrs — Verizon Sr. Java Full Stack, Equifax Full Stack Developer, Polaris; MS CIS Christian Brothers U.; BE ECE Anurag G. of Inst.).

## Phases & workflow

Phase 0 plan → 1 dashboard → 2 ingestion → 3 tailoring → 4 automation → 5 orchestration.
One feature branch per phase; PRs only after user approval + passing tests; PRs are never auto-merged.

## Run (Phase 1+)

```bash
uv venv && uv sync                 # create venv, install deps
cp .env.example .env               # add your LLM key (needed from Phase 3)
uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
uv run pytest -q                   # run tests
```

Open http://127.0.0.1:8000/ — the dashboard.

**Seed your profile from a resume file** (uploads + parses it, fills profile/skills/history/education and starter screening answers):

```bash
uv run python scripts/seed_profile.py path/to/resume.docx
```

## Phase 2 — Job ingestion

Two sources (no others, per spec):

1. **hiring-without-whiteboards** — the curated company list is parsed into the `companies` table (name, website, careers URL, region). Each company's ATS is then discovered by probing public JSON APIs first (Greenhouse `/jobs`, Lever `/postings`, Ashby, Workday, Rippling) and HTML careers pages as fallback, with polite delays. Companies whose ATS has a public job feed (Greenhouse/Lever today) have their listings pulled and stored in `jobs`.
2. **hiring.cafe** — the aggregator is fetched and its `__NEXT_DATA__` JSON parsed into structured jobs, then followed through to the original employer/ATS apply URL.

**Filtering** (`ingestion/filters.py`, pure functions): SWE title match, region (US = remote + onsite, EU = remote-only), 4+ YOE when stated, and US roles must not *explicitly* rule out visa sponsorship. Region detection handles country codes, country names, and bare city names (Greenhouse feeds give city-only, e.g. "Amsterdam").

**Dedup**: `sha1(source|apply_url)` (fallback `source|company|title`); re-ingests update in place.

**Known limitation — hiring.cafe bot wall**: hiring.cafe sits behind Cloudflare and returns 403 to automated HTTP requests (and to headless browser sessions). We do **not** bypass CAPTCHAs/bot detection. Fallback: open the page in a normal browser, copy the page source, and paste it into the "Manual fallback" box on the Jobs page — it parses exactly like the live fetch. The Greenhouse/Lever direct feeds are unaffected and work fully automated.

**Dashboard pages**: `/companies` (regional breakdown + ATS discovery + feed-pull controls) and `/jobs` (filter pass/fail list, per-job detail, hiring.cafe pull + manual paste, re-run-filters button).

Verified on 2026-11-02: 766 companies parsed (244 US / 275 EU), 25 companies ATS-checked (3 Greenhouse, 1 Ashby, 0 errors), 245 jobs ingested from 3 Greenhouse feeds, 15 passing filters after the city-geo refinement.
