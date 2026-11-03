from backend.app.ingestion.filters import job_passes_filters, regions_of, stack_match_score


def job(**kw):
    base = {
        "title": "Software Engineer II",
        "company": "Acme",
        "location": "Austin, Texas, United States",
        "countries": ["US"],
        "remote": True,
        "visa_sponsorship": True,
        "yoe": None,
        "commitment": ["Full Time"],
        "apply_url": "https://acme.example/apply",
        "source": "hiringcafe",
    }
    base.update(kw)
    return base


def test_keeps_us_java_swe():
    keep, reasons = job_passes_filters(job(title="Java Software Engineer II", yoe=4))
    assert keep is True, reasons


def test_drops_eu_onsite():
    keep, reasons = job_passes_filters(
        job(title="Software Engineer", location="Berlin, Germany", countries=["DE"], remote=False)
    )
    assert keep is False
    assert any("not remote" in r for r in reasons)


def test_keeps_eu_remote():
    keep, reasons = job_passes_filters(
        job(title="Backend Engineer", location="Remote (Germany)", countries=["DE"], remote=True,
            visa_sponsorship=None)
    )
    assert keep is True, reasons


def test_drops_outside_us_eu():
    keep, reasons = job_passes_filters(
        job(title="Software Engineer", location="Mumbai, India", countries=["IN"], remote=True)
    )
    assert keep is False
    assert any("outside" in r for r in reasons)


def test_drops_non_swe_title():
    keep, reasons = job_passes_filters(job(title="Marketing Manager"))
    assert keep is False
    assert any("SWE" in r for r in reasons)


def test_drops_entry_level_yoe():
    keep, reasons = job_passes_filters(job(title="Software Engineer", yoe=2))
    assert keep is False
    assert any("YOE" in r for r in reasons)


def test_drops_explicitly_non_sponsoring_us():
    keep, reasons = job_passes_filters(job(visa_sponsorship=False))
    assert keep is False
    assert any("sponsorship" in r for r in reasons)


def test_keeps_us_when_sponsorship_unknown():
    keep, reasons = job_passes_filters(job(visa_sponsorship=None, remote=False))
    assert keep is True, reasons


def test_regions_of():
    assert regions_of(job()) == (True, False)
    assert regions_of(job(countries=["FR"], location="Paris, France")) == (False, True)
    assert regions_of(job(countries=[], location="Singapore")) == (False, False)


def test_regions_of_city_only_locations():
    # Greenhouse-style feeds give bare city names, no country code
    assert regions_of(job(location="Amsterdam", countries=[])) == (False, True)
    assert regions_of(job(location="Berlin", countries=[])) == (False, True)
    assert regions_of(job(location="Austin, TX", countries=[])) == (True, False)
    assert regions_of(job(location="New York, NY", countries=[])) == (True, False)
    assert regions_of(job(location="Singapore", countries=[])) == (False, False)


def test_stack_match_score():
    high = job(title="Java Full Stack Engineer (Spring, React, AWS, SQL)")
    assert stack_match_score(high) >= 3
    assert stack_match_score(job(title="Software Engineer")) == 0  # no stack words
