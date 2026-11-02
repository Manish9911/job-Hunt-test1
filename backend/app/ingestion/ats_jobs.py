"""Fetch job listings from ATS endpoints discovered during ATS detection."""
from __future__ import annotations

import httpx


def fetch_greenhouse_jobs(endpoint: str, client: httpx.Client) -> list[dict]:
    """endpoint: boards-api.greenhouse.io/v1/boards/<slug>/jobs"""
    r = client.get(endpoint, params={"content": 1})
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(
            {
                "title": j.get("title") or "",
                "company": data.get("board", {}).get("name", ""),
                "company_website": data.get("board", {}).get("absolute_url", ""),
                "location": (j.get("location") or {}).get("name") or "",
                "countries": [],
                "remote": None,
                "visa_sponsorship": None,
                "commitment": [],
                "yoe": None,
                "salary_min": None,
                "salary_max": None,
                "salary_currency": "USD",
                "apply_url": j.get("absolute_url") or "",
                "source": "greenhouse",
                "hiringcafe_id": "",
                "indexed_at": j.get("first_published"),
            }
        )
    return jobs


def fetch_lever_jobs(endpoint: str, client: httpx.Client) -> list[dict]:
    """endpoint: api.lever.co/v0/postings/<slug>?mode=json"""
    r = client.get(endpoint)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data if isinstance(data, list) else []:
        jobs.append(
            {
                "title": j.get("text") or "",
                "company": j.get("categories", {}).get("company") or "",
                "company_website": "",
                "location": j.get("workLocation") or "",
                "countries": [],
                "remote": "remote" in (j.get("workLocation") or "").lower(),
                "visa_sponsorship": None,
                "commitment": [],
                "yoe": None,
                "salary_min": None,
                "salary_max": None,
                "salary_currency": "USD",
                "apply_url": j.get("hostedUrl") or "",
                "source": "lever",
                "hiringcafe_id": "",
                "indexed_at": j.get("createdAt"),
            }
        )
    return jobs


def fetch_jobs_for_endpoint(ats: str, endpoint: str, client: httpx.Client) -> list[dict]:
    if ats == "greenhouse" and endpoint:
        return fetch_greenhouse_jobs(endpoint, client)
    if ats == "lever" and endpoint:
        return fetch_lever_jobs(endpoint, client)
    return []
