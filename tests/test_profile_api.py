def test_profile_created_and_updated(client):
    r = client.get("/api/profile")
    assert r.status_code == 200
    assert r.json()["full_name"] == ""

    r = client.put("/api/profile", json={
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "+1 555-123-4567",
        "location": "Austin, TX",
        "summary": "Backend engineer.",
        "work_authorization": "US work visa (H1B)",
        "sponsorship_needed": True,
        "target_roles": "SWE II",
        "locations": ["US remote", "EU remote"],
        "min_salary": "$140k",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["full_name"] == "Jane Doe"
    assert body["locations"] == ["US remote", "EU remote"]
    assert client.get("/api/profile").json()["email"] == "jane@example.com"


def test_profile_accepts_csv_locations_from_form(client):
    r = client.put("/api/profile", json={"full_name": "A", "locations": "US remote, EU remote,"})
    assert r.json()["locations"] == ["US remote", "EU remote"]


def test_profile_checkbox_string_booleans(client):
    r = client.put("/api/profile", json={"full_name": "A", "sponsorship_needed": "false"})
    assert r.json()["sponsorship_needed"] is False
    r = client.put("/api/profile", json={"full_name": "A", "sponsorship_needed": "true"})
    assert r.json()["sponsorship_needed"] is True
