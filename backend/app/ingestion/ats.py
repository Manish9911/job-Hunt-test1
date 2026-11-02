"""Discover each company's careers page and ATS type.

Strategy (cheapest signal first):
1. Public ATS JSON APIs (Greenhouse boards API, Lever API) — a 200 both
   identifies the ATS AND gives a job-feed endpoint.
2. HTML probes of well-known ATS hosts (Ashby, Greenhouse boards, Lever,
   Workday).
3. Common careers-page paths on the company's own domain.

Polite by construction: one request at a time, caller controls the delay.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

PROBE_TIMEOUT = 10.0


def classify_ats_from_url(url: str) -> str:
    """Classify an ATS purely from the URL (pure function)."""
    u = url.lower()
    if "greenhouse" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "myworkdayjobs.com" in u:
        return "workday"
    if "rippling" in u:
        return "rippling"
    if "jobvite" in u:
        return "jobvite"
    if "smartrecruiters" in u:
        return "smartrecruiters"
    if "workable" in u:
        return "workable"
    if "adp.com" in u or "adp.ca" in u:
        return "adp"
    if "jibe.com" in u or "icims" in u:
        return "icims"
    return "other"


def slug_from_domain(domain: str) -> str:
    """Best-effort ATS slug: domain without TLD and www prefix."""
    d = domain.lower().split(":")[0]
    d = re.sub(r"^www\.", "", d)
    for tld in ("co.uk", "com.au", "co.in", "com.br", "com.sg"):
        if d.endswith("." + tld):
            d = d[: -(len(tld) + 1)]
            break
    else:
        if "." in d:
            d = d.rsplit(".", 1)[0]
    return d.rstrip(".")


def detect_company_ats(domain: str, client: httpx.Client) -> dict:
    """Return {ats, careers_url, jobs_endpoint}. Mutates nothing."""
    slug = slug_from_domain(domain)

    # 1) public JSON APIs
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if r.status_code == 200:
        return {
            "ats": "greenhouse",
            "careers_url": f"https://boards.greenhouse.io/{slug}",
            "jobs_endpoint": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        }
    r = client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if r.status_code == 200:
        return {
            "ats": "lever",
            "careers_url": f"https://{slug}.jobs.lever.co",
            "jobs_endpoint": f"https://api.lever.co/v0/postings/{slug}?mode=json",
        }

    # 2) HTML probes on known ATS hosts
    probes = [
        ("ashby", f"https://jobs.ashbyhq.com/{slug}"),
        ("greenhouse", f"https://boards.greenhouse.io/{slug}"),
        ("lever", f"https://{slug}.jobs.lever.co"),
        ("workday", f"https://{slug}.wd1.myworkdayjobs.com/"),
    ]
    for ats, url in probes:
        try:
            r = client.get(url)
            if r.status_code == 200:
                return {"ats": ats, "careers_url": url, "jobs_endpoint": None}
        except httpx.HTTPError:
            continue

    # 3) careers page on own domain
    for url in (
        f"https://{domain}/careers",
        f"https://{domain}/careers/",
        f"https://{domain}/jobs",
        f"https://{domain}/open-roles",
        f"https://careers.{domain}/",
    ):
        try:
            r = client.get(url)
            if r.status_code == 200:
                return {"ats": "unknown", "careers_url": url, "jobs_endpoint": None}
        except httpx.HTTPError:
            continue

    return {"ats": "unknown", "careers_url": None, "jobs_endpoint": None}


def ats_of_careers_url(careers_url: str | None) -> str:
    """If a URL is already known, classify it (for display/Phase 4)."""
    if not careers_url:
        return "unknown"
    ats = classify_ats_from_url(careers_url)
    return ats if ats != "other" else "unknown"


def registrable_domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return host
