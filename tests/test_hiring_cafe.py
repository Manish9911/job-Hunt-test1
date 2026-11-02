import json

import pytest

from backend.app.ingestion.hiring_cafe import extract_hits, parse_hiring_cafe_html


def _hit(title, company, apply_url, countries, worktype, visa, yoe=None, source="greenhouse"):
    return {
        "job_information": {"title": title, "job_title_raw": title},
        "v5_processed_job_data": {
            "company_name": company,
            "formatted_workplace_location": "United States" if "US" in countries else "Berlin, Germany",
            "workplace_countries": countries,
            "workplace_type": worktype,
            "visa_sponsorship": visa,
            "min_industry_and_role_yoe": yoe,
            "commitment": ["Full Time"],
            "yearly_min_compensation": 150,
            "yearly_max_compensation": 180,
            "listed_compensation_currency": "USD",
            "listed_compensation_frequency": "Yearly",
        },
        "attributed_org": {"name": company, "website": f"{company.lower()}.com"},
        "apply_url": apply_url,
        "source": source,
        "objectID": f"obj-{title.lower().replace(' ', '-')}",
        "hc_indexed_at": 1730000000,
    }


def _html(hits):
    payload = json.dumps({"props": {"pageProps": {"ssrHits": hits, "ssrPage": 0}}})
    return (
        "<html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
        "</body></html>"
    )


def test_parse_jobs_from_next_data():
    hits = [
        _hit("Software Engineer II - Java", "Acme", "https://acme.jobs.greenhouse.io/123",
             ["US"], "Remote", True, yoe=4),
        _hit("Marketing Specialist", "Beta", "https://beta.example/apply",
             ["US"], "Onsite", False),
    ]
    jobs = parse_hiring_cafe_html(_html(hits))
    assert len(jobs) == 2
    swe = jobs[0]
    assert swe["title"] == "Software Engineer II - Java"
    assert swe["company"] == "Acme"
    assert swe["apply_url"] == "https://acme.jobs.greenhouse.io/123"
    assert swe["remote"] is True
    assert swe["visa_sponsorship"] is True
    assert swe["yoe"] == 4
    assert swe["countries"] == ["US"]
    assert swe["salary_min"] == 150
    assert jobs[1]["remote"] is False


def test_worldwide_ok_treated_as_remote():
    hit = _hit("Senior SWE", "Acme", "https://x", ["US"], "Onsite", None)
    hit["v5_processed_job_data"]["is_workplace_worldwide_ok"] = True
    jobs = parse_hiring_cafe_html(_html([hit]))
    assert jobs[0]["remote"] is True


def test_missing_blob_raises():
    with pytest.raises(ValueError):
        extract_hits("<html><body>no data</body></html>")


def test_empty_title_skipped():
    hits = [_hit("", "Acme", "https://x", ["US"], "Remote", None)]
    assert parse_hiring_cafe_html(_html(hits)) == []
