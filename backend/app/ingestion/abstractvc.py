"""jobs.abstractvc.com (Abstract Ventures board) job ingestion.

The site is a JS app; the job data comes from its JSON API
``POST /api-boards/search-jobs`` which requires:

1. a ``session`` cookie set by ``GET /jobs``, and
2. an ``x-csrf-token`` header whose value is embedded in the page HTML
   (``csrfToken`` in a <meta> tag / inline script).

Without both the API answers 412. Pagination is cursor-based: each response
carries ``meta.sequence`` for the next page. Server-side filtering is
available — ``query.jobTypes: ["software-engineer"]`` cuts the ~2.4k job
board down to the SWE set, which is all jobflow wants.

We never bypass anything ourselves; if the API keeps refusing we raise
``IngestionBlocked`` and the user can retry later.
"""
from __future__ import annotations

import re
import time

import httpx

from .hiring_cafe import IngestionBlocked

BASE = "https://jobs.abstractvc.com"
SEARCH_API = BASE + "/api-boards/search-jobs"
BOARD_ID = "abstract-ventures"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

API_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": BASE + "/jobs",
}

# Only software-engineer roles — the board has ~2.4k jobs across all functions.
DEFAULT_QUERY = {"promoteFeatured": True, "jobTypes": ["software-engineer"]}
PAGE_SIZE = 50
MAX_PAGES = 60  # hard stop against a runaway cursor (~3000 jobs)

# The CSRF token is embedded in the landing page.
_CSRF_RE = re.compile(
    r'name="csrf-token"[^>]*content="([^"]+)"|csrfToken["\']?\s*[:=]\s*["\']([^"\']+)',
    re.I,
)

# Country names -> ISO codes we can expect on this board.
_COUNTRY_CODES = {
    "united states": "US",
    "united kingdom": "GB",
    "netherlands": "NL",
    "germany": "DE",
    "france": "FR",
    "ireland": "IE",
    "sweden": "SE",
    "denmark": "DK",
    "finland": "FI",
    "spain": "ES",
    "portugal": "PT",
    "belgium": "BE",
    "austria": "AT",
    "switzerland": "CH",
    "poland": "PL",
    "czech": "CZ",
    "hungary": "HU",
    "romania": "RO",
    "italy": "IT",
    "norway": "NO",
    "estonia": "EE",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "slovakia": "SK",
    "slovenia": "SI",
    "croatia": "HR",
    "greece": "GR",
    "bulgaria": "BG",
    "remote united states": "US",
}


def make_client(timeout: float = 90.0) -> httpx.Client:
    return httpx.Client(headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=True)


def bootstrap_token(client: httpx.Client, attempts: int = 2) -> str:
    """GET /jobs, take the session cookie, and return the embedded CSRF token.

    Raises IngestionBlocked if the page or the token cannot be obtained.
    """
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            r = client.get(BASE + "/jobs")
            if r.status_code == 200:
                m = _CSRF_RE.search(r.text)
                if m:
                    token = m.group(1) or m.group(2)
                    if token:
                        return token
                last_err = ValueError("page loaded but no csrf token found")
            else:
                last_err = httpx.HTTPStatusError(
                    f"HTTP {r.status_code}", request=r.request, response=r
                )
        except httpx.HTTPError as exc:
            last_err = exc
        if i < attempts - 1:
            time.sleep(5.0)
    raise IngestionBlocked(
        f"jobs.abstractvc.com refused the bootstrap request ({last_err}). "
        "Retry later or check the site in a browser to confirm it is up."
    )


def search_page(
    client: httpx.Client,
    token: str,
    query: dict | None = None,
    size: int = PAGE_SIZE,
    sequence: str | None = None,
) -> dict:
    """One POST to the search API. Returns the parsed JSON body."""
    meta: dict = {"size": size}
    if sequence:
        meta["sequence"] = sequence
    payload = {
        "meta": meta,
        "board": {"id": BOARD_ID, "isParent": True},
        "query": query if query is not None else DEFAULT_QUERY,
        "grouped": False,
    }
    headers = {**API_HEADERS, "x-csrf-token": token}
    r = client.post(SEARCH_API, headers=headers, json=payload)
    if r.status_code in (401, 403, 412):
        raise IngestionBlocked(
            f"search API refused the request (HTTP {r.status_code}). "
            "The site's bot protection may have tightened — retry later."
        )
    r.raise_for_status()
    return r.json()


def fetch_all_jobs(
    client: httpx.Client,
    token: str,
    query: dict | None = None,
    page_size: int = PAGE_SIZE,
    delay: float = 1.0,
    max_pages: int = MAX_PAGES,
) -> list[dict]:
    """Page through the cursor until it runs out. Returns raw job dicts."""
    jobs: list[dict] = []
    seen: set[str] = set()
    sequence: str | None = None
    for _ in range(max_pages):
        body = search_page(client, token, query=query, size=page_size, sequence=sequence)
        page_jobs = body.get("jobs") or []
        for j in page_jobs:
            key = (j.get("applyUrl") or j.get("url") or "").split("?")[0]
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            jobs.append(j)
        meta = body.get("meta") or {}
        sequence = meta.get("sequence")
        if not page_jobs or not sequence or len(page_jobs) < page_size:
            break
        time.sleep(delay)
    return jobs


def _label(value) -> str:
    """API values are either plain strings or {id,label,value} objects."""
    if isinstance(value, dict):
        return str(value.get("label") or value.get("value") or "").strip()
    return str(value or "").strip()


def countries_of(j: dict) -> list[str]:
    """ISO codes derived from the free-form location strings."""
    parts: list[str] = []
    for x in (j.get("locations") or []):
        parts.append(_label(x))
    for x in (j.get("normalizedLocations") or []):
        parts.append(_label(x))
    text = " ".join(parts).lower()
    found: list[str] = []
    for name, code in _COUNTRY_CODES.items():
        if name in text and code not in found:
            found.append(code)
    return found


def parse_abstractvc_job(j: dict) -> dict:
    """Normalize one API job object into jobflow's canonical job dict (pure)."""
    raw_locs = list(j.get("normalizedLocations") or j.get("locations") or [])
    locs: list[str] = []
    for x in raw_locs:
        t = _label(x).strip()
        if t:
            locs.append(t)
    # de-dup preserving order
    locs = list(dict.fromkeys(locs))
    location = " · ".join(locs)

    salary = j.get("salary") or {}
    period = _label(salary.get("period")).lower()
    currency = _label(salary.get("currency")) or "USD"
    salary_min = salary.get("minValue")
    salary_max = salary.get("maxValue")
    # Keep only annual figures; monthly/hourly would be a false comparison.
    if period not in ("", "year", "yearly", "annual"):
        salary_min = salary_max = None

    remote = bool(j.get("remote")) if j.get("remote") is not None else None
    if j.get("hybrid") and not remote:
        remote = False  # explicit hybrid = on-site component, not remote

    # Tags that both display and feed the deterministic scorer:
    # seniority + skill labels (title + these strings = the "JD text").
    tags: list[str] = []
    for s in j.get("jobSeniorities") or []:
        t = _label(s)
        if t and t not in tags:
            tags.append(t)
    for s in list(j.get("requiredSkills") or []) + list(j.get("preferredSkills") or []):
        t = _label(s)
        if t and t not in tags and len(tags) < 14:
            tags.append(t)

    apply_url = (j.get("applyUrl") or j.get("url") or "").split("?")[0]

    ts = j.get("timeStamp")
    indexed_at = ts
    if isinstance(ts, str):
        indexed_at = ts.replace("T", " ").rstrip("Z")

    return {
        "title": (j.get("title") or "").strip(),
        "company": (j.get("companyName") or j.get("companyId") or "").strip(),
        "company_website": (j.get("companyDomain") or "").strip(),
        "location": location,
        "countries": countries_of(j),
        "remote": remote,
        "visa_sponsorship": None,  # board does not state sponsorship either way
        "commitment": tags,
        "yoe": None,  # seniority labels are in commitment; no numeric claim
        "salary_min": int(salary_min) if salary_min else None,
        "salary_max": int(salary_max) if salary_max else None,
        "salary_currency": currency,
        "apply_url": apply_url,
        "source": "abstractvc",
        "hiringcafe_id": str(j.get("jobId") or ""),
        "indexed_at": indexed_at,
    }


def parse_abstractvc_response(body: dict) -> list[dict]:
    """Pure: normalize a whole search-API response body."""
    out: list[dict] = []
    for j in body.get("jobs") or []:
        parsed = parse_abstractvc_job(j)
        if parsed["title"]:
            out.append(parsed)
    return out


def total_available(client: httpx.Client, token: str, query: dict | None = None) -> int:
    """Server-side job count for a query (used for progress UI, not required)."""
    body = search_page(client, token, query=query, size=1)
    return int(body.get("total") or 0)
