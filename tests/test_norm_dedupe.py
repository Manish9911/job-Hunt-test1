"""Cross-board dedupe via the source-agnostic normalized key (norm_key).

The old unique_key is source+URL scoped, so the same posting resurfacing from
a different board (hiring.cafe vs the company's own ATS feed) double-stored.
norm_key = sha1(slug(company)|slug(title)|slug(location)) catches those.
"""
from backend.app.db.models import Job
from backend.app.ingestion.service import job_norm_key, store_jobs


def _job(company="Acme", title="Senior Java Developer (Remote)",
         location="Tampa, Florida, United States",
         source="greenhouse", apply_url="https://acme.example/apply/1"):
    return {
        "title": title,
        "company": company,
        "location": location,
        "source": source,
        "apply_url": apply_url,
        "remote": True,
        "visa_sponsorship": True,
        "commitment": ["Full Time"],
        "salary_min": 150000,
        "salary_max": 180000,
        "salary_currency": "USD",
        "countries": ["US"],
    }


def test_job_norm_key_normalizes():
    a = job_norm_key(_job())
    b = job_norm_key(_job(title="senior java developer - remote",
                          apply_url="https://other.example/x",
                          source="hiringcafe"))
    assert a == b and a != ""


def test_job_norm_key_differs_across_companies_and_titles():
    base = job_norm_key(_job())
    assert job_norm_key(_job(company="Beta")) != base
    assert job_norm_key(_job(title="Junior Java Developer (Remote)")) != base
    assert job_norm_key(_job(location="Berlin, Germany")) != base


def test_job_norm_key_empty_when_missing():
    assert job_norm_key({"title": "X", "company": ""}) == ""
    assert job_norm_key({"company": "Acme", "title": ""}) == ""


def test_cross_source_dedupe(client):
    db = client.app.state.SessionLocal()
    try:
        r1 = store_jobs(db, [_job()])
        assert r1 == {"seen": 1, "added": 1, "duplicates_skipped": 0, "passing_filters": 1}

        # Same posting via another board: new URL, new source, sloppy title.
        r2 = store_jobs(db, [_job(source="hiringcafe",
                                  apply_url="https://hiringcafe.com/job/999",
                                  title="senior java developer — remote")])
        assert r2["added"] == 0
        assert r2["duplicates_skipped"] == 1
        assert db.query(Job).count() == 1

        # Different company, same title: must NOT dedupe.
        r3 = store_jobs(db, [_job(company="Beta Corp",
                                  apply_url="https://beta.example/apply/2")])
        assert r3["added"] == 1
        assert db.query(Job).count() == 2

        # Same job object twice in one batch: second must skip (no autoflush).
        r4 = store_jobs(db, [_job(company="Gamma", apply_url="https://g.example/1"),
                             _job(company="Gamma", apply_url="https://g.example/1")])
        assert r4["added"] == 1
        assert r4["duplicates_skipped"] == 1
    finally:
        db.close()
