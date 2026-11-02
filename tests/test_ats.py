import httpx

from backend.app.ingestion.ats import classify_ats_from_url, detect_company_ats, slug_from_domain


def test_classify_ats_from_url():
    assert classify_ats_from_url("https://acme.jobs.greenhouse.io") == "greenhouse"
    assert classify_ats_from_url("https://boards-api.greenhouse.io/v1/boards/acme/jobs") == "greenhouse"
    assert classify_ats_from_url("https://acme.jobs.lever.co") == "lever"
    assert classify_ats_from_url("https://jobs.ashbyhq.com/acme") == "ashby"
    assert classify_ats_from_url("https://acme.wd1.myworkdayjobs.com/en_US/Acme") == "workday"
    assert classify_ats_from_url("https://ats.rippling.com/acme-careers/jobs") == "rippling"
    assert classify_ats_from_url("https://acme.com/careers") == "other"


def test_slug_from_domain():
    assert slug_from_domain("acme.com") == "acme"
    assert slug_from_domain("www.acme.co.uk") == "acme"
    assert slug_from_domain("acme.com:443") == "acme"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5.0)


def test_detect_greenhouse_via_public_api():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404)

    found = detect_company_ats("acme.com", _client(handler))
    assert found["ats"] == "greenhouse"
    assert found["jobs_endpoint"] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    assert "boards.greenhouse.io/acme" in found["careers_url"]


def test_detect_lever_via_public_api():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(404)
        if request.url.host == "api.lever.co":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    found = detect_company_ats("beta.io", _client(handler))
    assert found["ats"] == "lever"
    assert found["jobs_endpoint"] == "https://api.lever.co/v0/postings/beta?mode=json"


def test_detect_ashby_probe():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in ("boards-api.greenhouse.io", "api.lever.co"):
            return httpx.Response(404)
        if request.url.host == "jobs.ashbyhq.com":
            return httpx.Response(200, text="<html>ashby</html>")
        return httpx.Response(404)

    found = detect_company_ats("gamma.dev", _client(handler))
    assert found["ats"] == "ashby"
    assert found["careers_url"] == "https://jobs.ashbyhq.com/gamma"


def test_falls_back_to_careers_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host in ("boards-api.greenhouse.io", "api.lever.co",
                                "jobs.ashbyhq.com", "delta.jobs.lever.co",
                                "delta.wd1.myworkdayjobs.com",
                                "boards.greenhouse.io"):
            return httpx.Response(404)
        if str(request.url).endswith("/careers"):
            return httpx.Response(200, text="<html>careers</html>")
        return httpx.Response(404)

    found = detect_company_ats("delta.com", _client(handler))
    assert found["ats"] == "unknown"
    assert found["careers_url"] == "https://delta.com/careers"


def test_nothing_found():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    found = detect_company_ats("nowhere.xyz", _client(handler))
    assert found == {"ats": "unknown", "careers_url": None, "jobs_endpoint": None}
