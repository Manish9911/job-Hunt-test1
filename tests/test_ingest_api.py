import json

from backend.app.ingestion import service
from tests.test_hiring_cafe import _hit, _html


def test_manual_html_ingest_and_dedup(client):
    hits = [
        _hit("Software Engineer II - Java", "Acme", "https://acme.example/apply/1",
             ["US"], "Remote", True, yoe=4),
        _hit("Senior Backend Engineer", "Beta", "https://beta.example/apply/2",
             ["US"], "Remote", True, yoe=5),
        _hit("Marketing Specialist", "Gamma", "https://gamma.example/apply/3",
             ["US"], "Onsite", False),
    ]
    html = _html(hits)
    r = client.post("/api/ingest/jobs/hiring-cafe/manual", json={"html": html})
    assert r.status_code == 200, r.text
    body = r.json()
    stored = body["stored"]
    assert stored["seen"] == 3
    assert stored["added"] == 3
    assert stored["passing_filters"] == 2  # marketing dropped

    # second post of the same page: everything deduplicated
    r2 = client.post("/api/ingest/jobs/hiring-cafe/manual", json={"html": html})
    assert r2.json()["stored"]["added"] == 0
    assert r2.json()["stored"]["duplicates_skipped"] == 3

    jobs = client.get("/api/jobs").json()
    assert len(jobs) == 3
    passing = client.get("/api/jobs", params={"passing": True}).json()
    assert len(passing) == 2
    marketing = [j for j in jobs if "Marketing" in j["title"]][0]
    assert marketing["passes_filters"] is False
    assert marketing["filter_reasons"]


def test_manual_html_too_small(client):
    r = client.post("/api/ingest/jobs/hiring-cafe/manual", json={"html": "<html></html>"})
    assert r.status_code == 400


def test_jobs_count(client):
    r = client.get("/api/jobs/count")
    assert r.json() == {"total": 0, "passing": 0}


def test_companies_upsert_via_service(client):
    db = client.app.state.SessionLocal()
    try:
        raw = [
            {"name": "Adyen", "website": "https://www.adyen.com", "locations": ["Amsterdam, NL"],
             "remote": False, "region": "eu", "notes": "visa sponsorship"},
            {"name": "Acme", "website": "https://acme.com", "locations": ["Remote"],
             "remote": True, "region": "other", "notes": ""},
        ]
        result = service.upsert_companies(db, raw)
        assert result == {"total": 2, "added": 2, "updated": 0}
        # re-upsert same names: no duplicates
        result2 = service.upsert_companies(db, raw)
        assert result2 == {"total": 2, "added": 0, "updated": 2}

        listing = client.get("/api/companies").json()
        assert len(listing) == 2
        adyen = [c for c in listing if c["name"] == "Adyen"][0]
        assert adyen["region"] == "eu"
        assert adyen["ats"] == "unknown"

        eu_only = client.get("/api/companies", params={"region": "eu"}).json()
        assert [c["name"] for c in eu_only] == ["Adyen"]
    finally:
        db.close()


def test_unique_key_stable():
    j = {"source": "hiringcafe", "apply_url": "https://x.example/1", "company": "A", "title": "T"}
    assert service.job_unique_key(j) == service.job_unique_key(dict(j))
    other = dict(j, apply_url="https://x.example/2")
    assert service.job_unique_key(j) != service.job_unique_key(other)
