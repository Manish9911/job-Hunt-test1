"""hiring.cafe (hiringcafe.com) job ingestion.

The site sits behind a Cloudflare-style bot check: plain requests often get
403. We (a) send normal browser headers with spaced retries and backoff, and
(b) if we are still blocked, we raise IngestionBlocked — the dashboard then
offers a MANUAL fallback (paste the page HTML) instead of trying to bypass
anything. Per project hard rules we never solve challenges ourselves.

Job data comes from the page's embedded __NEXT_DATA__ JSON
(props.pageProps.ssrHits[]), which includes the original employer
`apply_url` — exactly the "follow listing to the original ATS page" step.
"""
from __future__ import annotations

import json
import re
import time

import httpx

BASE = "https://hiringcafe.com"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

# Listing pages to ingest (all default to US scope unless a filter says otherwise)
DEFAULT_PAGES = [
    "/jobs",                  # latest jobs (US)
    "/jobs/f/visa-sponsorship",
    "/jobs/f/remote",
]


class IngestionBlocked(Exception):
    """Bot check / repeated 403s. User should paste HTML or retry later."""


def make_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=True)


def fetch_page(client: httpx.Client, path: str, attempts: int = 3, base_delay: float = 10.0) -> str:
    """GET a page with polite backoff. Raises IngestionBlocked if still 403."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            r = client.get(BASE + path)
            if r.status_code == 200:
                return r.text
            last_err = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        except httpx.HTTPError as exc:
            last_err = exc
        if i < attempts - 1:
            time.sleep(base_delay * (i + 1))
    raise IngestionBlocked(
        f"hiring.cafe kept refusing automated requests ({last_err}). "
        "Open the page in your browser and use the manual paste box on the "
        "Jobs page instead."
    )


def extract_hits(html: str) -> list[dict]:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise ValueError("No __NEXT_DATA__ blob found in page HTML")
    data = json.loads(m.group(1))
    page_props = (data.get("props") or {}).get("pageProps") or {}
    return page_props.get("ssrHits") or []


def parse_hiring_cafe_html(html: str) -> list[dict]:
    """Parse a saved/handled page into normalized job dicts (pure, testable)."""
    jobs: list[dict] = []
    for hit in extract_hits(html):
        ji = hit.get("job_information") or {}
        v5 = hit.get("v5_processed_job_data") or {}
        org = hit.get("attributed_org") or {}
        title = (ji.get("title") or ji.get("job_title_raw") or "").strip()
        if not title:
            continue
        yearly_min = v5.get("yearly_min_compensation") or 0
        yearly_max = v5.get("yearly_max_compensation") or 0
        freq = (v5.get("listed_compensation_frequency") or "").lower()
        # hiring.cafe expresses money as annualized thousands for Yearly listings
        salary_min = int(yearly_min) if yearly_min else None
        salary_max = int(yearly_max) if yearly_max else None
        if freq in ("hourly",):
            salary_min = None  # keep it simple; hourly listings not salary-compared
            salary_max = None
        jobs.append(
            {
                "title": title,
                "company": (org.get("name") or v5.get("company_name") or "").strip(),
                "company_website": (org.get("website") or v5.get("company_website") or "").strip(),
                "location": v5.get("formatted_workplace_location") or "",
                "countries": v5.get("workplace_countries") or [],
                "remote": _is_remote(v5),
                "visa_sponsorship": _tri(v5.get("visa_sponsorship")),
                "commitment": v5.get("commitment") or [],
                "yoe": v5.get("min_industry_and_role_yoe"),
                "salary_min": salary_min,
                "salary_max": salary_max,
                "salary_currency": v5.get("listed_compensation_currency") or "USD",
                "apply_url": hit.get("apply_url") or "",
                "source": hit.get("source") or "hiringcafe",
                "hiringcafe_id": hit.get("objectID") or "",
                "indexed_at": hit.get("hc_indexed_at"),
            }
        )
    return jobs


def _is_remote(v5: dict) -> bool | None:
    if v5.get("is_workplace_worldwide_ok"):
        return True
    wtype = (v5.get("workplace_type") or "").lower()
    if wtype in ("remote", "hybrid"):
        return True
    if wtype == "onsite":
        return False
    return None


def _tri(value) -> bool | None:
    if value is True:
        return True
    if value is False:
        return False
    return None
