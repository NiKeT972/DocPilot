import base64
import hashlib
import io

import pytest
from conftest import SESSION
from fakes import FakeFastLLM, make_services, settings_for_tests

from docpilot.services import set_services

SHA = hashlib.sha256(b"file-1").hexdigest()


def page_text(n: int) -> str:
    return f"Page {n} discusses topic number {n} in detail. " * 8


def batch(pages, *, first=False, last=False, doc_id=None, total=100, name="big.pdf", sha=SHA, size=1_000_000):
    return {
        "session_id": SESSION,
        "doc_id": doc_id,
        "doc_name": name,
        "sha256": sha,
        "size_bytes": size,
        "total_pages": total,
        "pages": [{"page": p, "text": page_text(p)} for p in pages],
        "first_batch": first,
        "last_batch": last,
    }


def upload_in_batches(client, total=100, size=40, **kw):
    doc_id = None
    for start in range(1, total + 1, size):
        pages = list(range(start, min(total, start + size - 1) + 1))
        r = client.post(
            "/api/ingest",
            json=batch(pages, first=start == 1, last=pages[-1] == total, doc_id=doc_id, total=total, **kw),
        )
        assert r.status_code == 200, r.text
        doc_id = r.json()["doc_id"]
    return r.json()


def test_ingest_batches_keep_pages_and_count_chunks(client, services):
    out = upload_in_batches(client, total=100)
    assert out["pages"] == 100 and out["chunks"] >= 100
    chunks = [c for c in services.store.chunks if c.doc_id == out["doc_id"]]
    assert {c.page for c in chunks} == set(range(1, 101))
    idx = sorted(c.chunk_index for c in chunks)
    assert idx == list(range(len(chunks))), "chunk_index continues across batches"
    docs = client.get("/api/documents", params={"session_id": SESSION}).json()["documents"]
    assert docs[0]["chunks"] == out["chunks"] and docs[0]["pages"] == 100


def test_duplicate_upload_replaces_old_copy(client, services):
    first = upload_in_batches(client, total=10)
    second = upload_in_batches(client, total=10)
    assert second["doc_id"] != first["doc_id"]
    docs = client.get("/api/documents", params={"session_id": SESSION}).json()["documents"]
    assert [d["id"] for d in docs] == [second["doc_id"]]
    assert not [c for c in services.store.chunks if c.doc_id == first["doc_id"]]
    # Same name but different content is a different document.
    upload_in_batches(client, total=10, sha=hashlib.sha256(b"other").hexdigest())
    assert len(client.get("/api/documents", params={"session_id": SESSION}).json()["documents"]) == 2


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"size_bytes": 51 * 1024 * 1024}, 413),
        ({"total_pages": 1001}, 413),
        ({"pages": [{"page": p, "text": "x"} for p in range(1, 42)]}, 413),
        ({"pages": [{"page": 101, "text": "x"}]}, 422),
        ({"sha256": "nothex"}, 422),
        ({"doc_name": ""}, 422),
    ],
)
def test_ingest_limits(client, override, code):
    body = {**batch([1], first=True), **override}
    assert client.post("/api/ingest", json=body).status_code == code


def test_files_per_session_cap(client):
    set_services(make_services(settings=settings_for_tests(max_files_per_session=2)))
    for i in range(2):
        r = client.post("/api/ingest", json=batch([1], first=True, total=1, sha=f"{i:064x}"))
        assert r.status_code == 200
    r = client.post("/api/ingest", json=batch([1], first=True, total=1, sha=f"{9:064x}"))
    assert r.status_code == 409 and r.json()["code"] == "too_many_files"


def test_pages_per_session_cap(client):
    set_services(make_services(settings=settings_for_tests(max_pages_per_session=150)))
    assert client.post("/api/ingest", json=batch([1], first=True, total=100)).status_code == 200
    r = client.post("/api/ingest", json=batch([1], first=True, total=60, sha=f"{2:064x}"))
    assert r.status_code == 409 and "150-page" in r.json()["error"]


def test_followup_batch_needs_valid_doc(client):
    assert client.post("/api/ingest", json=batch([2])).status_code == 422
    r = client.post("/api/ingest", json=batch([2], doc_id="00000000-0000-4000-8000-000000000000"))
    assert r.status_code == 404


def test_ocr_pages_capped_per_file(client):
    set_services(make_services(settings=settings_for_tests(max_ocr_pages_per_file=3)))
    body = batch([1, 2, 3], first=True, total=10)
    for p in body["pages"]:
        p["ocr"] = True
    r = client.post("/api/ingest", json=body)
    assert r.status_code == 200 and r.json()["ocr_pages"] == 3
    body2 = batch([4], doc_id=r.json()["doc_id"], total=10)
    body2["pages"][0]["ocr"] = True
    r2 = client.post("/api/ingest", json=body2)
    assert r2.status_code == 413 and r2.json()["code"] == "ocr_limit"


def test_empty_pdf_warns(client):
    body = batch([1, 2], first=True, last=True, total=2)
    for p in body["pages"]:
        p["text"] = "  "
    r = client.post("/api/ingest", json=body)
    assert r.status_code == 200 and r.json()["chunks"] == 0 and "No readable text" in r.json()["warning"]


def test_oversized_request_body_is_413(client):
    huge = batch([1], first=True)
    huge["pages"][0]["text"] = "x" * 59_000
    huge["pages"] = huge["pages"] * 50  # ~3 MB, over the 2.5 MB batch budget
    r = client.post("/api/ingest", json=huge)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"


def test_ingest_rate_limit(client):
    set_services(make_services(settings=settings_for_tests(rate_ingest_per_day=2)))
    out = client.post("/api/ingest", json=batch([1], first=True, total=3)).json()
    assert client.post("/api/ingest", json=batch([2], doc_id=out["doc_id"], total=3)).status_code == 200
    r = client.post("/api/ingest", json=batch([3], doc_id=out["doc_id"], total=3))
    assert r.status_code == 429 and "upload limit" in r.json()["error"]


# ---------------------------------------------------------------- OCR endpoint
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


class FakeOCR:
    def __init__(self):
        self.calls = 0

    async def transcribe(self, image, mime="image/jpeg"):
        self.calls += 1
        return "Scanned page text"


def test_ocr_endpoint(client, services):
    services.ocr = FakeOCR()
    img = base64.b64encode(JPEG).decode()
    r = client.post("/api/ocr", json={"session_id": SESSION, "image_base64": "data:image/jpeg;base64," + img})
    assert r.status_code == 200 and r.json() == {"text": "Scanned page text"}
    bad = base64.b64encode(b"GIF89a" + b"\x00" * 200).decode()
    assert client.post("/api/ocr", json={"session_id": SESSION, "image_base64": bad}).status_code == 422
    assert client.post("/api/ocr", json={"session_id": SESSION, "image_base64": "!" * 200}).status_code == 422


def test_ocr_not_configured_and_rate_limited(client):
    svc = make_services(settings=settings_for_tests(rate_ocr_per_day=1))
    set_services(svc)
    img = base64.b64encode(JPEG).decode()
    assert client.post("/api/ocr", json={"session_id": SESSION, "image_base64": img}).status_code == 503
    svc.ocr = FakeOCR()
    assert client.post("/api/ocr", json={"session_id": SESSION, "image_base64": img}).status_code == 200
    assert client.post("/api/ocr", json={"session_id": SESSION, "image_base64": img}).status_code == 429


# ---------------------------------------------------------------- /api/upload
def make_pdf(pages: list[str]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for text in pages:
        y = 800
        for line in [text[i : i + 90] for i in range(0, len(text), 90)]:
            c.drawString(40, y, line)
            y -= 14
        c.showPage()
    c.save()
    return buf.getvalue()


def test_upload_server_side_parse(client, services):
    pdf = make_pdf([page_text(1), page_text(2), ""])
    r = client.post(
        "/api/upload", data={"session_id": SESSION}, files={"file": ("report.pdf", pdf, "application/pdf")}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pages"] == 3 and body["chunks"] >= 2 and body["scanned_pages_skipped"] == 1
    pages = {c.page for c in services.store.chunks}
    assert pages == {1, 2}
    r2 = client.post(
        "/api/upload", data={"session_id": SESSION}, files={"file": ("report.pdf", pdf, "application/pdf")}
    )
    assert r2.json()["replaced"] == body["doc_id"]


def test_upload_rejects_non_pdf_and_large(client):
    r = client.post(
        "/api/upload", data={"session_id": SESSION}, files={"file": ("x.pdf", b"hello", "application/pdf")}
    )
    assert r.status_code == 415
    big = b"%PDF" + b"0" * (4 * 1024 * 1024 + 10)
    r = client.post(
        "/api/upload", data={"session_id": SESSION}, files={"file": ("x.pdf", big, "application/pdf")}
    )
    assert r.status_code == 413


# ---------------------------------------------------------------- suggestions
def test_suggest_uses_latest_doc(client, services):
    services.fast_llm = FakeFastLLM(suggest='{"questions": ["Q one?", "Q two?", "Q three?", "Q four?"]}')
    assert client.post("/api/suggest", json={"session_id": SESSION}).json()["questions"] == []
    upload_in_batches(client, total=5, name="a.pdf")
    second = upload_in_batches(client, total=5, name="b.pdf", sha=f"{7:064x}")
    body = client.post("/api/suggest", json={"session_id": SESSION}).json()
    assert body["doc_id"] == second["doc_id"] and body["questions"] == ["Q one?", "Q two?", "Q three?"]


def test_suggest_falls_back_on_garbage(client, services):
    services.fast_llm = FakeFastLLM(suggest="not json")
    upload_in_batches(client, total=2)
    qs = client.post("/api/suggest", json={"session_id": SESSION}).json()["questions"]
    assert len(qs) == 3 and all(q.endswith((".", "?")) for q in qs)


# ---------------------------------------------------------------- cron
def test_cron_cleanup_requires_secret(client):
    set_services(make_services(settings=settings_for_tests(cron_secret="s3cret")))
    assert client.post("/api/cron/cleanup").status_code == 401
    assert client.get("/api/cron/cleanup", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.get("/api/cron/cleanup", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_cron_disabled_without_secret(client):
    assert client.get("/api/cron/cleanup", headers={"Authorization": "Bearer "}).status_code == 401


async def test_cleanup_removes_old_docs(services):
    from datetime import UTC, datetime, timedelta

    from fakes import add_doc

    d = await add_doc(services, SESSION, "old.pdf", ["Old content for cleanup testing."])
    d.created_at = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    await add_doc(services, SESSION, "new.pdf", ["New content stays around."])
    out = await services.store.cleanup(7)
    assert out["documents"] == 1
    assert [x.name for x in await services.store.list_docs(SESSION)] == ["new.pdf"]
    assert all(c.doc_id != d.id for c in services.store.chunks)
