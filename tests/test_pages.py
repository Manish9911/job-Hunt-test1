def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_all_pages_render(client):
    for path in ("/", "/profile", "/resumes", "/skills", "/experience", "/education", "/answers"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "jobflow" in r.text
