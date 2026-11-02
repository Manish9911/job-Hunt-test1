from backend.app.ingestion.hwow import domain_of, parse_hwow_readme

SAMPLE = """
# Hiring without whiteboards

## A

- [Adyen](https://www.adyen.com) | Amsterdam, NL | Take-home assignment; provides visa sponsorship
- [Able](https://able.co/careers) | Lima, PE / Remote | Coding interview
- [Acme](https://acme.com) | San Francisco, CA; New York, NY | Pair programming
- [Beta Labs](https://beta.example) | Mumbai, India | Phone screen
- [Gamma](https://gamma.io) | Remote | On-the-job assessment
"""


def test_parse_basic_fields():
    companies = parse_hwow_readme(SAMPLE)
    by_name = {c["name"]: c for c in companies}
    assert len(companies) == 5
    adyen = by_name["Adyen"]
    assert adyen["website"] == "https://www.adyen.com"
    assert adyen["locations"] == ["Amsterdam, NL"]
    assert adyen["remote"] is False
    assert adyen["region"] == "eu"
    assert "visa sponsorship" in adyen["notes"]


def test_parse_remote_and_multi_location():
    companies = {c["name"]: c for c in parse_hwow_readme(SAMPLE)}
    able = companies["Able"]
    assert able["remote"] is True
    assert able["locations"] == ["Lima, PE", "Remote"]
    assert able["region"] == "other"  # Peru

    acme = companies["Acme"]
    assert acme["region"] == "us"
    assert acme["remote"] is False
    assert len(acme["locations"]) == 2


def test_region_classification():
    companies = {c["name"]: c for c in parse_hwow_readme(SAMPLE)}
    assert companies["Gamma"]["region"] == "other"  # Remote only, no country
    assert companies["Beta Labs"]["region"] == "other"  # India
    assert companies["Adyen"]["region"] == "eu"


def test_dedup_by_name():
    text = "- [Dup](https://a.com) | Remote | x\n- [Dup](https://b.com) | Remote | y\n"
    companies = parse_hwow_readme(text)
    assert len(companies) == 1
    assert companies[0]["website"] == "https://a.com"


def test_domain_of():
    assert domain_of("https://www.adyen.com") == "adyen.com"
    assert domain_of("https://able.co/careers") == "able.co"
