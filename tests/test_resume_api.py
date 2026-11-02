from docx import Document

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(tmp_path, name="sample.docx"):
    doc = Document()
    doc.add_paragraph("Jane Doe")
    doc.add_paragraph("jane@example.com | +1 555-123-4567")
    doc.add_paragraph("Technical Skills:")
    doc.add_paragraph("Cloud Platforms: AWS")
    p = tmp_path / name
    doc.save(str(p))
    return p


def test_upload_and_list(client, tmp_path):
    p = _docx_bytes(tmp_path)
    with p.open("rb") as f:
        r = client.post("/api/resumes", files={"file": ("jane.docx", f, MIME)}, data={"label": "Master"})
    assert r.status_code == 201
    body = r.json()
    assert body["is_master"] is True
    assert body["content"]["name"] == "Jane Doe"

    listing = client.get("/api/resumes").json()
    assert len(listing) == 1
    assert listing[0]["label"] == "Master"
    assert listing[0]["is_master"] is True

    one = client.get(f"/api/resumes/{body['id']}").json()
    assert one["content"]["skills"]["Cloud Platforms"] == ["AWS"]


def test_first_resume_is_master_then_switch(client, tmp_path):
    p = _docx_bytes(tmp_path)
    with p.open("rb") as f:
        r1 = client.post("/api/resumes", files={"file": ("one.docx", f, MIME)}).json()
    with p.open("rb") as f:
        r2 = client.post("/api/resumes", files={"file": ("two.docx", f, MIME)}).json()
    assert r1["is_master"] is True
    assert r2["is_master"] is False

    switched = client.post(f"/api/resumes/{r1['id']}/master").json()
    assert switched["is_master"] is True
    listing = {row["id"]: row["is_master"] for row in client.get("/api/resumes").json()}
    assert listing[r1["id"]] is True
    assert listing[r2["id"]] is False


def test_download_returns_original_bytes(client, tmp_path):
    p = _docx_bytes(tmp_path)
    original = p.read_bytes()
    with p.open("rb") as f:
        r = client.post("/api/resumes", files={"file": ("jane.docx", f, MIME)}).json()
    r = client.get(f"/api/resumes/{r['id']}/download")
    assert r.status_code == 200
    assert r.content == original


def test_delete_fallback_master(client, tmp_path):
    p = _docx_bytes(tmp_path)
    with p.open("rb") as f:
        r1 = client.post("/api/resumes", files={"file": ("one.docx", f, MIME)}).json()
    with p.open("rb") as f:
        r2 = client.post("/api/resumes", files={"file": ("two.docx", f, MIME)}).json()
    assert client.delete(f"/api/resumes/{r1['id']}").status_code == 200
    listing = {row["id"]: row["is_master"] for row in client.get("/api/resumes").json()}
    assert listing == {r2["id"]: True}


def test_unsupported_type_rejected(client, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hi", encoding="utf-8")
    with p.open("rb") as f:
        r = client.post("/api/resumes", files={"file": ("notes.txt", f, "text/plain")})
    assert r.status_code == 400


def test_404_for_other_profile_resume(client):
    r = client.get("/api/resumes/999")
    assert r.status_code == 404
