"""Run DocPilot locally with offline fakes (no API keys) — for UI development and screenshots.

    python scripts/dev_fake.py            # http://localhost:8000

The fake agent cites the passages it retrieves, so citations, the viewer and every
route can be exercised without spending credits. Never used in production.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
os.environ["DOTENV_DISABLED"] = "1"
for var in (
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "TAVILY_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "VERCEL",
):
    os.environ.pop(var, None)

import uvicorn  # noqa: E402
from fakes import FakeAnswerLLM, FakeFastLLM, FakeWeb, make_services, settings_for_tests  # noqa: E402

from docpilot.services import set_services  # noqa: E402
from docpilot.web import WebResult  # noqa: E402


def grade(user: str):
    q = user.split("Excerpts:")[0].lower()
    if any(w in q for w in ("latest", "today", "current", "news", "2026")):
        return {"relevant": [], "fully_answers": False, "missing": "recent information"}
    n = len(re.findall(r"<<<PDF_EXCERPT id=", user))
    if "compare" in q or "versus" in q:
        return {"relevant": [1], "fully_answers": False, "missing": "outside context"}
    return {"relevant": list(range(1, min(n, 3) + 1)), "fully_answers": True, "missing": ""}


def answer(messages):
    user = messages[-1]["content"]
    pdf = re.findall(r"<<<PDF_EXCERPT id=(P\d+)[^>]*>>>\n(.*?)\n<<<END_PDF_EXCERPT>>>", user, re.S)
    web = re.findall(r"<<<WEB_RESULT id=(W\d+)[^>]*>>>\n(.*?)\n<<<END_WEB_RESULT>>>", user, re.S)
    parts = []
    for tag, text in pdf[:3]:
        sentence = re.split(r"(?<=[.!?])\s", text.strip())[0][:220]
        parts.append(f"- {sentence} [{tag}]")
    out = ""
    if parts:
        out = "Here's what your documents say:\n\n" + "\n".join(parts)
    if web:
        out += ("\n\n" if out else "") + f"According to web sources, {web[0][1][:160]} [{web[0][0]}]."
    return out or "The context doesn't contain the answer."


web = FakeWeb(
    [
        WebResult(
            "Reuters — Markets today",
            "https://www.reuters.com/markets/",
            "Markets were mixed today as investors weighed new data.",
        ),
        WebResult(
            "Wikipedia",
            "https://en.wikipedia.org/wiki/Retrieval-augmented_generation",
            "Retrieval-augmented generation combines search with LLMs.",
        ),
    ]
)


class FakeOCR:
    async def transcribe(self, image: bytes, mime: str = "image/jpeg") -> str:
        return f"[OCR] Scanned page transcribed by the fake OCR ({len(image)} bytes). The scanned code is SCAN-42."


svc = make_services(
    settings=settings_for_tests(rate_chat_per_hour=1000),
    fast=FakeFastLLM(grade=grade, rewrite="latest news on the topic"),
    answer=FakeAnswerLLM(answer),
    web=web,
)
svc.ocr = FakeOCR()
set_services(svc)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")), log_level="warning")
