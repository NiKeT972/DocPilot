"""DocPilot API (FastAPI). Routes only; logic lives in the `docpilot` package.

Vercel deploys this as a single Python Function (zero-config: `app` in `app.py`).
Static files in `public/` are served by Vercel's CDN; locally we mount them here.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from docpilot import __version__
from docpilot.config import get_settings
from docpilot.graph import run_chat
from docpilot.httpx_retry import UpstreamError
from docpilot.logs import error as log_error
from docpilot.logs import event as log_event
from docpilot.logs import request_id_var
from docpilot.ratelimit import HOUR, RateLimited
from docpilot.services import get_services

SESSION_RE = r"^[A-Za-z0-9-]{8,64}$"
UUIDISH_RE = r"^[A-Za-z0-9-]{8,64}$"
PUBLIC_DIR = Path(__file__).parent / "public"

# Fail loudly at startup on unsafe configs (e.g. Vercel without Supabase).
get_settings().validate()

app = FastAPI(title="DocPilot", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

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


# ------------------------------------------------------------------ static (local dev)
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(PUBLIC_DIR / "index.html")


if PUBLIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
