"""Tests for jobs.abstractvc.com ingestion (parser + pagination; no network)."""
from __future__ import annotations

import pytest

from backend.app.ingestion.abstractvc import (
    IngestionBlocked,
    bootstrap_token,
    countries_of,
    fetch_all_jobs,
    parse_abstractvc_job,
    parse_abstractvc_response,
    search_page,
)


# ---------------------------------------------------------------------------
# Fixtures: real shapes captured from the live API (anonymized where needed)
# ---------------------------------------------------------------------------

JOB_STAFF = {
    "title": "Staff Software Engineer (AI Agents)",
    "companyName": "Traba",
    "companyId": "Traba",
    "companyDomain": "traba.work",
    "applyUrl": "https://jobs.ashbyhq.com/traba/954204a7?utm_source=jobs.abstractvc.com",
    "url": "https://jobs.ashbyhq.com/traba/954204a7",
    "jobId": "954204a7",
    "timeStamp": "2026-11-02T00:00:00Z",
    "remote": False,
    "hybrid": False,
    "locations": [
        "New York City, NY, United States",
        "San Francisco, California, United States",
    ],
    "normalizedLocations": [
        {"id": "San Francisco, California", "label": "San Francisco, California",
         "value": "San Francisco, California"},
        {"id": "New York, New York", "label": "New York, New York",
         "value": "New York, New York"},
    ],
    "salary": {
        "period": {"label": "Year", "value": "year"},
        "minValue": 240000,
        "maxValue": 300000,
        "currency": {"label": "USD", "value": "USD"},
        "isOriginal": True,
    },
    "jobSeniorities": [{"id": "senior", "label": "Senior", "value": "senior"}],
    "requiredSkills": [
        {"id": "resume:ERP", "label": "ERP", "value": "resume:ERP"},
        {"id": "resume:REST", "label": "REST", "value": "resume:REST"},
    ],
    "preferredSkills": [
        {"id": "resume:Observability", "label": "Observability", "value": "resume:Observability"},
    ],
    "departments": ["Engineering"],
    "regions": [{"id": "North America", "label": "North America", "value": "North America"}],
}

JOB_REMOTE_EU = {
    "title": "Senior Software Engineer, Backend",
    "companyName": "Dust",
    "companyDomain": "dust.tt",
    "applyUrl": "https://jobs.ashbyhq.com/dust/1bb240b3",
    "jobId": "1bb240b3",
    "timeStamp": "2026-10-30T23:59:33Z",
    "remote": True,
    "hybrid": False,
    "locations": ["Remote, Europe"],
    "normalizedLocations": [
        {"id": "Amsterdam, Netherlands", "label": "Amsterdam, Netherlands",
         "value": "Amsterdam, Netherlands"},
    ],
    "salary": None,
    "jobSeniorities": ["Senior"],
    "requiredSkills": [],
    "preferredSkills": [
        {"id": "resume:Python", "label": "Python", "value": "resume:Python"},
        {"id": "resume:Kubernetes", "label": "Kubernetes", "value": "resume:Kubernetes"},
    ],
}

JOB_MONTHLY = {
    "title": "Software Engineer, Platform",
    "companyName": "Motion",
    "applyUrl": "https://jobs.gem.com/motion/abc123",
    "jobId": "abc123",
    "timeStamp": None,
    "remote": None,
    "hybrid": True,
    "locations": ["Montreal, Canada"],
    "normalizedLocations": ["Montreal, Canada"],
    "salary": {
        "period": {"label": "Month", "value": "month"},
        "minValue": 15000,
        "maxValue": 18000,
        "currency": {"label": "CAD", "value": "CAD"},
    },
    "jobSeniorities": [{"label": "Mid", "value": "mid"}],
    "requiredSkills": [],
    "preferredSkills": [],
}


# ---------------------------------------------------------------------------
# Pure parser tests
# ---------------------------------------------------------------------------

def test_parse_us_onsite_staff():
    j = parse_abstractvc_job(JOB_STAFF)
    assert j["title"] == "Staff Software Engineer (AI Agents)"
    assert j["company"] == "Traba"
    assert j["company_website"] == "traba.work"
    assert "United States" in " ".join(JOB_STAFF["locations"])
    assert "US" in j["countries"]
    assert j["remote"] is False
    assert j["visa_sponsorship"] is None
    assert j["salary_min"] == 240000
    assert j["salary_max"] == 300000
    assert j["salary_currency"] == "USD"
    assert j["apply_url"] == "https://jobs.ashbyhq.com/traba/954204a7"  # utm stripped
    assert j["source"] == "abstractvc"
    assert j["hiringcafe_id"] == "954204a7"
    assert "Senior" in j["commitment"]  # seniority tag present
    assert "REST" in j["commitment"]    # skill tag present (feeds scorer)
    assert j["location"] == "San Francisco, California · New York, New York"


def test_parse_remote_eu_no_salary():
    j = parse_abstractvc_job(JOB_REMOTE_EU)
    assert j["remote"] is True
    assert "NL" in j["countries"]
    assert j["salary_min"] is None and j["salary_max"] is None
    # string seniority labels handled too
    assert "Senior" in j["commitment"]


def test_monthly_salary_dropped_hybrid_not_remote():
    j = parse_abstractvc_job(JOB_MONTHLY)
    assert j["salary_min"] is None and j["salary_max"] is None
    assert j["remote"] is False  # hybrid => has on-site component
    assert "CA" not in j["countries"]  # Canada not US/EU
    assert j["indexed_at"] is None


def test_parse_response_skips_blank_titles():
    body = {"jobs": [JOB_STAFF, {**JOB_REMOTE_EU, "title": ""}]}
    out = parse_abstractvc_response(body)
    assert len(out) == 1


def test_countries_of_plain_and_lv_locations():
    j = {
        "locations": ["United Kingdom", "London"],
        "normalizedLocations": [{"value": "Dublin, Ireland"}],
    }
    assert set(countries_of(j)) == {"GB", "IE"}


# ---------------------------------------------------------------------------
# CSRF bootstrap (fake client, no network)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.request = None
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://x"),
                response=None,
            )


class _FakeBootstrapClient:
    def __init__(self, responses):
        self._responses = list(responses)

    def get(self, url):
        return self._responses.pop(0)


PAGE_WITH_META = '<html><meta name="csrf-token" content="tok-abc"></html>'
PAGE_WITH_VAR = '<html><script>const csrfToken = "tok-xyz";</script></html>'


def test_bootstrap_token_meta():
    c = _FakeBootstrapClient([_Resp(200, PAGE_WITH_META)])
    assert bootstrap_token(c) == "tok-abc"


def test_bootstrap_token_var_form():
    c = _FakeBootstrapClient([_Resp(200, PAGE_WITH_VAR)])
    assert bootstrap_token(c) == "tok-xyz"


def test_bootstrap_blocked_on_403():
    import httpx
    r = _Resp(403, "blocked")
    r.request = httpx.Request("GET", "https://x")
    c = _FakeBootstrapClient([r, r])
    with pytest.raises(IngestionBlocked):
        bootstrap_token(c, attempts=2)


# ---------------------------------------------------------------------------
# Pagination with a scripted fake search API
# ---------------------------------------------------------------------------

class _FakeSearchClient:
    def __init__(self, pages):
        self.pages = pages  # list of _JsonResp bodies
        self.calls = 0

    def post(self, url, headers=None, json=None):
        self.calls += 1
        return self.pages.pop(0)


class _JsonResp(_Resp):
    def json(self):
        import json as _json
        return _json.loads(self.text)


def _page(jobs, sequence, total=3):
    return {"jobs": jobs, "meta": {"size": 2, "sequence": sequence}, "total": total}


def test_fetch_all_jobs_follows_cursor_until_empty():
    import json as _json
    j1 = {**JOB_STAFF, "applyUrl": "https://a/1"}
    j2 = {**JOB_STAFF, "applyUrl": "https://a/2"}
    j3 = {**JOB_STAFF, "applyUrl": "https://a/3"}
    client = _FakeSearchClient([
        _JsonResp(200, _json.dumps(_page([j1, j2], "seq2"))),
        _JsonResp(200, _json.dumps(_page([j3], None))),
    ])
    out = fetch_all_jobs(client, "tok", delay=0, page_size=2)
    assert len(out) == 3
    assert client.calls == 2


def test_fetch_all_jobs_dedupes_by_apply_url():
    import json as _json
    j1 = {**JOB_STAFF, "applyUrl": "https://a/1"}
    client = _FakeSearchClient([
        _JsonResp(200, _json.dumps(_page([j1, {**j1, "jobId": "dup"}], None))),
    ])
    out = fetch_all_jobs(client, "tok", delay=0)
    assert len(out) == 1


def test_search_page_412_raises_blocked():
    client = _FakeSearchClient([_Resp(412, "precondition failed")])
    with pytest.raises(IngestionBlocked):
        search_page(client, "tok")
