"""DocPilot API (FastAPI). Routes only; logic lives in the `docpilot` package.

Vercel deploys this as a single Python Function (zero-config: `app` in `app.py`).
Static files in `public/` are served by Vercel's CDN; locally we mount them here.
"""

from __future__ import annotations

import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from docpilot import __version__
from docpilot.config import get_settings
from docpilot.graph import run_chat
from docpilot.httpx_retry import UpstreamError
from docpilot.ingest import BatchPage, IngestError, ingest_batch, ingest_pdf_bytes, suggest_questions
from docpilot.logs import error as log_error
from docpilot.logs import event as log_event
from docpilot.logs import request_id_var
from docpilot.ocr import InvalidImage, decode_image
from docpilot.ratelimit import DAY, HOUR, RateLimited
from docpilot.services import get_services

SESSION_RE = r"^[A-Za-z0-9-]{8,64}$"
UUIDISH_RE = r"^[A-Za-z0-9-]{8,64}$"
PUBLIC_DIR = Path(__file__).parent / "public"

# Fail loudly at startup on unsafe configs (e.g. Vercel without Supabase).
get_settings().validate()

app = FastAPI(title="DocPilot", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

_s = get_settings()
BODY_LIMITS = {
    "/api/ingest": _s.max_batch_bytes + 256_000,
    "/api/ocr": 3_000_000,
    "/api/upload": _s.max_upload_mb * 1024 * 1024 + 64_000,
    "/api/chat": 64_000,
}

SessionId = Annotated[str, Query(pattern=SESSION_RE)]


# ------------------------------------------------------------------ errors / middleware
class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        self.status = status
        self.message = message
        self.headers = headers or {}


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-vercel-id", "")[:64] or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    t0 = time.perf_counter()
    limit = BODY_LIMITS.get(request.url.path)
    if limit and request.method == "POST":
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            request_id_var.reset(token)
            return JSONResponse(
                {"error": "Request is too large. Batches must be under 2.5 MB.", "code": "payload_too_large"},
                status_code=413,
            )
    try:
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        if request.url.path.startswith("/api/"):
            log_event(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=round((time.perf_counter() - t0) * 1000),
            )
        return response
    finally:
        request_id_var.reset(token)


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError):
    return JSONResponse({"error": exc.message}, status_code=exc.status, headers=exc.headers)


@app.exception_handler(RateLimited)
async def _rate_limited(_: Request, exc: RateLimited):
    return JSONResponse(
        {"error": exc.message, "code": "rate_limited"},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.exception_handler(UpstreamError)
async def _upstream(_: Request, exc: UpstreamError):
    log_error("upstream error", exc_info=False, detail=str(exc), status=exc.status)
    return JSONResponse({"error": str(exc)}, status_code=502)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError):
    errs = []
    for e in exc.errors()[:5]:
        loc = ".".join(str(p) for p in e.get("loc", []) if p not in ("body", "query"))
        errs.append(f"{loc}: {e.get('msg', 'invalid')}" if loc else e.get("msg", "invalid"))
    return JSONResponse({"error": "Invalid request. " + "; ".join(errs)}, status_code=422)


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    log_error("unhandled error", error_type=type(exc).__name__)
    return JSONResponse(
        {"error": "Something went wrong on our side. Please try again.", "request_id": request_id_var.get()},
        status_code=500,
    )


def require_llm() -> None:
    missing = get_services().settings.missing_keys()
    if missing:
        raise ApiError(503, f"Server not configured: missing {', '.join(missing)}.")


# ------------------------------------------------------------------ models
class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_RE)
    question: str = Field(min_length=1, max_length=2000)
    history: list[HistoryItem] = Field(default_factory=list, max_length=6)
    allow_web: bool = False
    doc_ids: list[Annotated[str, Field(pattern=UUIDISH_RE)]] | None = Field(default=None, max_length=10)

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question is empty")
        return v

    @model_validator(mode="after")
    def _history_budget(self):
        if sum(len(h.content) for h in self.history) > 12_000:
            raise ValueError("history is too large")
        return self


# ------------------------------------------------------------------ routes
@app.get("/api/health")
async def health():
    s = get_services().settings
    return {
        "status": "ok",
        "version": __version__,
        "store": get_services().store.kind,
        "configured": not s.missing_keys(),
        "missing": s.missing_keys(),
        "web_enabled": s.web_enabled,
        "ocr_enabled": s.ocr_enabled,
        "models": {
            "answer": s.answer_model,
            "fast": s.fast_model,
            "embed": s.embed_model,
            "embed_dim": s.embed_dim,
            "ocr": s.ocr_model,
        },
        "limits": {
            "max_pdf_mb": s.max_pdf_mb,
            "max_pages": s.max_pages,
            "max_files_per_session": s.max_files_per_session,
            "max_pages_per_session": s.max_pages_per_session,
            "max_ocr_pages_per_file": s.max_ocr_pages_per_file,
            "max_batch_pages": s.max_batch_pages,
            "max_batch_bytes": s.max_batch_bytes,
            "max_upload_mb": s.max_upload_mb,
            "rate_chat_per_hour": s.rate_chat_per_hour,
            "rate_ingest_per_day": s.rate_ingest_per_day,
            "rate_ocr_per_day": s.rate_ocr_per_day,
        },
    }


@app.get("/api/documents")
async def list_documents(session_id: SessionId):
    docs = await get_services().store.list_docs(session_id)
    return {"documents": [d.public() for d in docs]}


@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: str, session_id: SessionId):
    if not 8 <= len(doc_id) <= 64:
        raise ApiError(404, "Document not found.")
    ok = await get_services().store.delete_doc(session_id, doc_id)
    if not ok:
        raise ApiError(404, "Document not found.")
    return {"deleted": doc_id}


@app.delete("/api/documents")
async def clear_documents(session_id: SessionId):
    n = await get_services().store.clear_session(session_id)
    return {"deleted": n}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    require_llm()
    svc = get_services()
    await svc.limiter.hit(
        request,
        "chat",
        svc.settings.rate_chat_per_hour,
        HOUR,
        session_id=req.session_id,
        friendly=f"You've asked {svc.settings.rate_chat_per_hour} questions this hour — that's the demo limit. "
        "Please try again later.",
    )
    deps = svc.deps()
    history = [h.model_dump() for h in req.history]
    timings: dict[str, int] = {}

    def events() -> AsyncIterator[dict]:
        return run_chat(
            deps,
            session_id=req.session_id,
            question=req.question,
            history=history,
            allow_web=req.allow_web,
            doc_ids=req.doc_ids,
            on_node_timing=lambda node, ms: timings.__setitem__(node, ms),
        )

    def log_done(done: dict) -> None:
        log_event(
            "chat",
            session=req.session_id[:8],
            route=done.get("route"),
            latency_ms=done.get("latency_ms"),
            nodes=timings,
            warnings=done.get("warnings"),
        )

    if "application/json" in request.headers.get(
        "accept", ""
    ) and "text/event-stream" not in request.headers.get("accept", ""):
        sources: list = []
        done: dict = {}
        async for ev in events():
            if ev["type"] == "sources":
                sources = ev["sources"]
            elif ev["type"] == "done":
                done = ev
        log_done(done)
        return {
            "answer": done.get("answer", ""),
            "route": done.get("route", "none"),
            "sources": sources,
            "trace": done.get("trace", []),
            "standalone": done.get("standalone"),
            "web_query": done.get("web_query"),
            "warnings": done.get("warnings", []),
            "latency_ms": done.get("latency_ms"),
        }

    async def stream() -> AsyncIterator[str]:
        yield ": stream open\n\n"
        try:
            async for ev in events():
                kind = ev.pop("type")
                if kind == "done":
                    log_done(ev)
                yield _sse(kind, ev)
        except UpstreamError as exc:
            log_error("chat upstream error", exc_info=False, detail=str(exc))
            yield _sse("error", {"message": str(exc)})
        except Exception as exc:
            log_error("chat failed", error_type=type(exc).__name__)
            yield _sse("error", {"message": "Something went wrong while answering. Please try again."})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ ingestion
class IngestPage(BaseModel):
    page: int = Field(ge=1, le=100_000)
    text: str = Field(default="", max_length=60_000)
    ocr: bool = False


class IngestRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_RE)
    doc_id: str | None = Field(default=None, pattern=UUIDISH_RE)
    doc_name: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    size_bytes: int = Field(ge=1)
    total_pages: int = Field(ge=1)
    pages: list[IngestPage] = Field(max_length=200)
    first_batch: bool = False
    last_batch: bool = False

    @field_validator("doc_name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = "".join(ch for ch in v if ch.isprintable()).strip().replace("/", "_").replace("\\", "_")
        if not v:
            raise ValueError("doc_name is empty")
        return v


@app.exception_handler(IngestError)
async def _ingest_error(_: Request, exc: IngestError):
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)


def require_embedder():
    svc = get_services()
    if svc.embedder is None:
        raise ApiError(503, "Server not configured: missing GEMINI_API_KEY.")
    return svc


@app.post("/api/ingest")
async def ingest(req: IngestRequest, request: Request):
    svc = require_embedder()
    s = svc.settings
    await svc.limiter.hit(
        request,
        "ingest",
        s.rate_ingest_per_day,
        DAY,
        session_id=req.session_id,
        friendly="You've hit today's upload limit for this demo. Please try again tomorrow.",
    )
    result = await ingest_batch(
        svc.store,
        svc.embedder,
        s,
        session_id=req.session_id,
        doc_id=req.doc_id,
        doc_name=req.doc_name,
        sha256=req.sha256.lower(),
        size_bytes=req.size_bytes,
        total_pages=req.total_pages,
        pages=[BatchPage(p.page, p.text, p.ocr) for p in req.pages],
        first_batch=req.first_batch,
    )
    log_event(
        "ingest",
        session=req.session_id[:8],
        doc=result.doc.id[:8],
        pages=len(req.pages),
        chunks=result.batch_chunks,
        first=req.first_batch,
        last=req.last_batch,
    )
    return result.to_dict(req.last_batch)


class OcrRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_RE)
    image_base64: str = Field(min_length=100, max_length=2_800_000)


@app.post("/api/ocr")
async def ocr(req: OcrRequest, request: Request):
    svc = get_services()
    if svc.ocr is None:
        raise ApiError(503, "OCR is not configured on this server.")
    try:
        image, mime = decode_image(req.image_base64)
    except InvalidImage as exc:
        raise ApiError(422, str(exc)) from exc
    await svc.limiter.hit(
        request,
        "ocr",
        svc.settings.rate_ocr_per_day,
        DAY,
        session_id=req.session_id,
        friendly=f"OCR is limited to {svc.settings.rate_ocr_per_day} scanned pages per day in this demo.",
    )
    text = await svc.ocr.transcribe(image, mime)
    return {"text": text[:60_000]}


@app.post("/api/upload")
async def upload(request: Request, session_id: str = Form(pattern=SESSION_RE), file: UploadFile = File(...)):
    svc = require_embedder()
    s = svc.settings
    limit = s.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ApiError(
            413, f"Direct uploads are limited to {s.max_upload_mb} MB. Use the web app for larger files."
        )
    if not data.startswith(b"%PDF"):
        raise ApiError(415, "Only PDF files are supported.")
    await svc.limiter.hit(request, "ingest", s.rate_ingest_per_day, DAY, session_id=session_id)
    name = "".join(ch for ch in (file.filename or "document.pdf") if ch.isprintable())[:200] or "document.pdf"
    return await ingest_pdf_bytes(svc.store, svc.embedder, s, session_id=session_id, name=name, data=data)


class SuggestRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_RE)
    doc_id: str | None = Field(default=None, pattern=UUIDISH_RE)


@app.post("/api/suggest")
async def suggest(req: SuggestRequest):
    svc = get_services()
    return await suggest_questions(svc.store, svc.fast_llm, req.session_id, req.doc_id)


# ------------------------------------------------------------------ cron
@app.api_route("/api/cron/cleanup", methods=["GET", "POST"])
async def cron_cleanup(request: Request):
    """Vercel Cron calls this daily with `Authorization: Bearer $CRON_SECRET`."""
    svc = get_services()
    secret = svc.settings.cron_secret
    auth = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(auth, f"Bearer {secret}"):
        raise ApiError(401, "Unauthorized.")
    result = await svc.store.cleanup(svc.settings.retention_days)
    log_event("cron_cleanup", **{k: v for k, v in (result or {}).items() if isinstance(v, int)})
    return {"ok": True, "deleted": result}


# ------------------------------------------------------------------ static (local dev)
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(PUBLIC_DIR / "index.html")


if PUBLIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
