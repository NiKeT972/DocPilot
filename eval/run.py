"""DocPilot evaluation harness.

    python -m eval.download                      # fetch the public PDFs
    python -m eval.run --base-url http://localhost:8000

Uploads the eval PDFs into a fresh session through the public API, asks every question in
`eval/questions.jsonl` through the JSON chat endpoint, and reports:

* routing accuracy   - expected vs actual route (pdf / hybrid / web / none)
* answer correctness - LLM-as-judge (the answer model, strict 0/1 rubric)
* citation page accuracy - does a cited PDF page contain the answer (gold pages verified by hand)
* latency p50 / p95  - end-to-end per question, measured by the client

The 6 current-events questions are asked twice: with web search on (expected route "web") and
with it off (expected route "none"). Results go to eval/results.md and eval/results.json.
Requires GROQ_API_KEY for the judge (the server under test needs its own keys).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docpilot.graph import extract_json  # noqa: E402
from docpilot.llm import GroqLLM  # noqa: E402

EVAL_DIR = Path(__file__).parent
PDFS = ["attention.pdf", "rag.pdf"]

JUDGE_PROMPT = """You are a strict grader for a question-answering system.
Score 1 only if the SYSTEM ANSWER correctly and directly answers the QUESTION. Rules:
- If an EXPECTED ANSWER is given, every key fact in it must be present and correct in the system answer \
(numbers must match; extra correct detail is fine). Missing or wrong key facts -> 0.
- If no expected answer is given (fast-changing current events), use the WEB SOURCES as ground truth: \
score 1 only if the answer is responsive and every claim is supported by those sources.
- An answer that says it cannot find the information scores 0 unless the expected answer says it is not available.
Reply with ONLY JSON: {"score": 0 or 1, "reason": "one short sentence"}"""


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def cited_pages(sources: list[dict], doc: str | None) -> list[int]:
    pages = [
        s["page"] for s in sources if s.get("type") == "pdf" and (doc is None or s.get("doc_name") == doc)
    ]
    return list(dict.fromkeys(pages))


async def judge(llm: GroqLLM, q: dict, result: dict) -> tuple[int, str]:
    web = "\n".join(
        f"- {s.get('title', '')}: {s.get('snippet', '')}" for s in result["sources"] if s.get("type") == "web"
    )
    msg = (
        f"QUESTION: {q['question']}\n"
        f"EXPECTED ANSWER: {q['expected_answer'] or '(none: judge against the web sources)'}\n"
        f"WEB SOURCES:\n{web or '(none)'}\n\nSYSTEM ANSWER:\n{result['answer']}"
    )
    for attempt in range(3):
        try:
            raw = await llm.complete(
                [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": msg}],
                max_tokens=300,
                json_mode=True,
            )
            obj = extract_json(raw) or {}
            if obj.get("score") in (0, 1, "0", "1"):
                return int(obj["score"]), str(obj.get("reason", ""))[:200]
        except Exception as exc:  # judge hiccup: retry, then record as unscored
            if attempt == 2:
                return 0, f"judge error: {exc}"
            await asyncio.sleep(5)
    return 0, "judge returned unparseable output"


async def run(args: argparse.Namespace) -> int:
    questions = [json.loads(line) for line in (EVAL_DIR / "questions.jsonl").read_text().splitlines() if line]
    if args.limit:
        questions = questions[: args.limit]
    session = f"eval-{uuid.uuid4().hex[:12]}"
    base = args.base_url.rstrip("/")
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20))
    health = (await client.get(f"{base}/api/health")).json()
    print(f"server {base}: store={health['store']} models={health['models']}")

    # 1) Ingest
    for name in PDFS:
        path = EVAL_DIR / "pdfs" / name
        if not path.exists():
            print(f"missing {path}; run `python -m eval.download` first", file=sys.stderr)
            return 1
        t0 = time.perf_counter()
        r = await client.post(
            f"{base}/api/upload",
            data={"session_id": session},
            files={"file": (name, path.read_bytes(), "application/pdf")},
        )
        r.raise_for_status()
        body = r.json()
        print(
            f"indexed {name}: {body['pages']} pages, {body['chunks']} chunks in {time.perf_counter() - t0:.1f}s"
        )
        await asyncio.sleep(args.upload_pause)  # let per-minute embedding quotas reset

    # 2) Ask
    runs = [(q, q["allow_web"], q["expected_route"]) for q in questions]
    runs += [(q, False, "none") for q in questions if q["category"] == "web"]
    judge_llm = GroqLLM(
        os.environ["GROQ_API_KEY"], os.getenv("ANSWER_MODEL", "openai/gpt-oss-120b"), temperature=0
    )
    rows = []
    for i, (q, web, expected) in enumerate(runs, 1):
        t0 = time.perf_counter()
        r = await client.post(
            f"{base}/api/chat",
            json={"session_id": session, "question": q["question"], "allow_web": web},
            headers={"Accept": "application/json"},
        )
        wall = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            print(f"[{i}/{len(runs)}] {q['id']} HTTP {r.status_code}: {r.text[:200]}")
            res = {"answer": "", "route": "error", "sources": [], "latency_ms": None}
        else:
            res = r.json()
        if expected == "none":
            score = int(res["route"] == "none")
            reason = "expected a not-found answer with web off"
        else:
            score, reason = await judge(judge_llm, q, res)
        pages = cited_pages(res["sources"], q.get("doc"))
        page_hit = None
        if q.get("pages") and pages:
            page_hit = any(p in q["pages"] for p in pages)
        row = {
            "id": q["id"] + ("" if web == q["allow_web"] else "-webOff"),
            "category": q["category"] if web == q["allow_web"] else "web-off",
            "question": q["question"],
            "allow_web": web,
            "expected_route": expected,
            "route": res["route"],
            "route_ok": res["route"] == expected,
            "correct": score,
            "judge_reason": reason,
            "gold_pages": q.get("pages"),
            "cited_pages": pages,
            "page_hit": page_hit,
            "latency_ms": round(wall),
            "server_ms": res.get("latency_ms"),
            "answer": res["answer"],
            "sources": [
                {k: s.get(k) for k in ("tag", "type", "doc_name", "page", "title", "url")}
                for s in res["sources"]
            ],
        }
        rows.append(row)
        print(
            f"[{i}/{len(runs)}] {row['id']:14} route {row['route']:6} (want {expected:6}) "
            f"correct={score} pages={pages} gold={q.get('pages')} {wall / 1000:.1f}s"
        )
        await asyncio.sleep(args.sleep)

    await client.delete(f"{base}/api/documents", params={"session_id": session})
    await client.aclose()
    write_reports(rows, health, args)
    return 0


def summarize(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    lat = [r["latency_ms"] for r in rows if r["latency_ms"]]
    with_pages = [r for r in rows if r["page_hit"] is not None]
    pdf_q = [r for r in rows if r["gold_pages"] and r["category"] in ("pdf", "hybrid")]
    return {
        "n": len(rows),
        "routing_accuracy": sum(r["route_ok"] for r in rows) / len(rows),
        "answer_correctness": sum(r["correct"] for r in rows) / len(rows),
        "citation_page_accuracy": (sum(r["page_hit"] for r in with_pages) / len(with_pages))
        if with_pages
        else 0,
        "pdf_questions_with_pdf_citation": len(with_pages),
        "pdf_questions": len(pdf_q),
        "p50_s": pct(lat, 0.5) / 1000,
        "p95_s": pct(lat, 0.95) / 1000,
        "by_category": {
            c: {
                "n": len(v),
                "routing": sum(r["route_ok"] for r in v) / len(v),
                "correct": sum(r["correct"] for r in v) / len(v),
                "p50_s": pct([r["latency_ms"] for r in v], 0.5) / 1000,
            }
            for c, v in by.items()
        },
        "confusion": Counter(f"{r['expected_route']}->{r['route']}" for r in rows),
    }


def write_reports(rows: list[dict], health: dict, args: argparse.Namespace) -> None:
    s = summarize(rows)
    (EVAL_DIR / "results.json").write_text(
        json.dumps(
            {"summary": {**s, "confusion": dict(s["confusion"])}, "rows": rows}, indent=2, ensure_ascii=False
        )
    )
    m = health["models"]
    lines = [
        "# DocPilot evaluation results",
        "",
        f"Run: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · server `{args.base_url}` · store `{health['store']}` · "
        f"answer `{m['answer']}` · grader/rewriter `{m['fast']}` · embeddings `{m['embed']}` ({m['embed_dim']}d) · "
        "web search Tavily",
        "",
        "Documents: *Attention Is All You Need* (arXiv:1706.03762, 15 pages) and *Retrieval-Augmented Generation for "
        "Knowledge-Intensive NLP Tasks* (arXiv:2005.11401, 19 pages), uploaded through the public API.",
        "",
        "## Summary",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Routing accuracy (expected vs actual route) | **{s['routing_accuracy']:.0%}** ({sum(r['route_ok'] for r in rows)}/{s['n']}) |",
        f"| Answer correctness (LLM-as-judge, strict 0/1) | **{s['answer_correctness']:.0%}** ({sum(r['correct'] for r in rows)}/{s['n']}) |",
        f"| Citation page accuracy (a cited page holds the answer) | **{s['citation_page_accuracy']:.0%}** "
        f"({sum(r['page_hit'] for r in rows if r['page_hit'] is not None)}/{s['pdf_questions_with_pdf_citation']} answers with PDF citations) |",
        f"| Latency p50 / p95 (end-to-end, client-measured) | **{s['p50_s']:.1f} s / {s['p95_s']:.1f} s** |",
        "",
        "## By category",
        "",
        "| Category | n | Routing | Correct | p50 latency |",
        "|---|---|---|---|---|",
    ]
    names = {
        "pdf": "Answerable from PDFs",
        "hybrid": "Needs PDF + web",
        "web": "Not in PDFs (web on)",
        "web-off": "Not in PDFs (web off -> expect 'none')",
    }
    for c in ("pdf", "hybrid", "web", "web-off"):
        if c in s["by_category"]:
            b = s["by_category"][c]
            lines.append(
                f"| {names[c]} | {b['n']} | {b['routing']:.0%} | {b['correct']:.0%} | {b['p50_s']:.1f} s |"
            )
    lines += [
        "",
        "Route confusion (expected -> actual): "
        + ", ".join(f"`{k}` x{v}" for k, v in sorted(s["confusion"].items())),
        "",
        "## Method",
        "",
        "- 30 questions written before any run (`eval/questions.jsonl`): 18 answerable from the PDFs with hand-verified "
        "gold pages, 6 needing PDF + web, 6 current-events questions not in the PDFs. The 6 current-events questions "
        "are asked again with web search off, expecting a 'not found' answer (36 runs total).",
        "- Correctness is judged by the answer model with a strict rubric against the expected answer; for "
        "fast-changing current events without a fixed answer, the judge checks the answer against the cited web "
        "sources. LLM judges are imperfect; per-question reasons are in `results.json`.",
        "- Citation page accuracy counts answers (PDF and hybrid questions) whose PDF citations include at least one "
        "gold page.",
        "- Latency is wall-clock time for the full non-streamed JSON request from the eval client, sequential, "
        "no caching. In the streaming UI the first tokens arrive earlier.",
        "- No prompts, thresholds or questions were tuned on these questions.",
        "",
        "## Per question",
        "",
        "| id | expected | actual | correct | cited pages | gold pages | latency |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['expected_route']} | {r['route']}{'' if r['route_ok'] else ' ✗'} | "
            f"{'✓' if r['correct'] else '✗'} | {r['cited_pages'] or '-'} | {r['gold_pages'] or '-'} | "
            f"{r['latency_ms'] / 1000:.1f} s |"
        )
    (EVAL_DIR / "results.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:22]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions (smoke run)")
    ap.add_argument("--sleep", type=float, default=2.0, help="pause between questions (Groq rate limits)")
    ap.add_argument(
        "--upload-pause", type=float, default=65, help="pause after each upload (embedding quotas)"
    )
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
