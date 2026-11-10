# DocPilot evaluation results

Run: 2026-11-10 12:54 UTC · server `http://localhost:8000` · store `supabase` · answer `openai/gpt-oss-120b` · grader/rewriter `openai/gpt-oss-20b` · embeddings `gemini-embedding-001` (768d) · web search Tavily

Documents: *Attention Is All You Need* (arXiv:1706.03762, 15 pages) and *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks* (arXiv:2005.11401, 19 pages), uploaded through the public API.

## Summary

| Metric | Result |
|---|---|
| Routing accuracy (expected vs actual route) | **89%** (32/36) |
| Answer correctness (LLM-as-judge, strict 0/1) | **92%** (33/36) |
| Citation page accuracy (a cited page holds the answer) | **96%** (23/24 answers with PDF citations) |
| Latency p50 / p95 (end-to-end, client-measured) | **7.1 s / 10.4 s** |

## By category

| Category | n | Routing | Correct | p50 latency |
|---|---|---|---|---|
| Answerable from PDFs | 18 | 100% | 94% | 5.2 s |
| Needs PDF + web | 6 | 33% | 67% | 8.6 s |
| Not in PDFs (web on) | 6 | 100% | 100% | 7.4 s |
| Not in PDFs (web off -> expect 'none') | 6 | 100% | 100% | 7.2 s |

Route confusion (expected -> actual): `hybrid->hybrid` x2, `hybrid->pdf` x4, `none->none` x6, `pdf->pdf` x18, `web->web` x6

## Run history (both runs reported)

| Run | Code | Routing | Correct | Citation pages | p50 / p95 |
|---|---|---|---|---|---|
| 1 | first full run | 83% (30/36) | 89% (32/36) | 100% (22/22) | 8.2 s / 12.3 s |
| 2 | after 2 bug fixes found in run 1 | **89% (32/36)** | **92% (33/36)** | **96% (23/24)** | **7.1 s / 10.4 s** |

Between runs, only bugs were fixed. The questions, answer keys, prompts and thresholds were not changed:
1. The answer model sometimes wrote citations with an invisible zero-width space (`[\u200bP1]`). Those tags were dropped, so 2 correct answers were labelled "Not found".
2. A Groq free-tier rate limit (429) during streaming wasn't retried, so one question returned an error.

Run 1's full output is kept in `results_run1.md` / `results_run1.json`.

## Known weaknesses (from run 2)

- **Hybrid routing is the weak spot (2/6).** For two-part questions ("X from the paper + Y from the web"), the fast grader often marks the PDF context as fully answering, so web search never runs. The answer then correctly says the second half isn't in the context (hyb-02, hyb-06). Twice, the answer model added outside facts without a `[W#]` tag, so the label stayed "From your PDFs" (hyb-04, hyb-05). Planned fix: decompose multi-part questions before grading, and drop untagged claims.
- **pdf-11:** marked wrong because the answer key also listed the WSJ-only score (91.3), which the question didn't ask for. The key is left unchanged.
- **pdf-04:** answer correct, but it cited the hyperparameter table (p.5/9) rather than the gold page 3.

## Method

- 30 questions written before any run (`eval/questions.jsonl`): 18 answerable from the PDFs with hand-verified gold pages, 6 needing PDF + web, 6 current-events questions not in the PDFs. The 6 current-events questions are asked again with web search off, expecting a 'not found' answer (36 runs total).
- Correctness is judged by the answer model with a strict rubric against the expected answer; for fast-changing current events without a fixed answer, the judge checks the answer against the cited web sources. LLM judges are imperfect; per-question reasons are in `results.json`.
- Citation page accuracy counts answers (PDF and hybrid questions) whose PDF citations include at least one gold page.
- Latency is wall-clock time for the full non-streamed JSON request from the eval client, sequential, no caching. In the streaming UI the first tokens arrive earlier.
- No prompts, thresholds or questions were tuned on these questions.

## Per question

| id | expected | actual | correct | cited pages | gold pages | latency |
|---|---|---|---|---|---|---|
| pdf-01 | pdf | pdf | ✓ | [8, 1] | [1, 8] | 5.3 s |
| pdf-02 | pdf | pdf | ✓ | [1] | [1, 8] | 3.0 s |
| pdf-03 | pdf | pdf | ✓ | [3] | [3] | 2.7 s |
| pdf-04 | pdf | pdf | ✓ | [5, 9] | [3] | 3.7 s |
| pdf-05 | pdf | pdf | ✓ | [5] | [5] | 3.2 s |
| pdf-06 | pdf | pdf | ✓ | [4] | [4] | 2.8 s |
| pdf-07 | pdf | pdf | ✓ | [6] | [6] | 2.8 s |
| pdf-08 | pdf | pdf | ✓ | [7] | [7] | 3.4 s |
| pdf-09 | pdf | pdf | ✓ | [8] | [8] | 2.9 s |
| pdf-10 | pdf | pdf | ✓ | [8] | [8] | 8.8 s |
| pdf-11 | pdf | pdf | ✗ | [10] | [10] | 7.2 s |
| pdf-12 | pdf | pdf | ✓ | [8] | [8] | 10.4 s |
| pdf-13 | pdf | pdf | ✓ | [4] | [4] | 7.1 s |
| pdf-14 | pdf | pdf | ✓ | [3, 4] | [3] | 10.5 s |
| pdf-15 | pdf | pdf | ✓ | [3] | [3] | 5.0 s |
| pdf-16 | pdf | pdf | ✓ | [6] | [6] | 12.7 s |
| pdf-17 | pdf | pdf | ✓ | [4] | [4, 8] | 8.5 s |
| pdf-18 | pdf | pdf | ✓ | [17] | [17] | 8.2 s |
| hyb-01 | hybrid | hybrid | ✓ | [1] | [1] | 10.3 s |
| hyb-02 | hybrid | pdf ✗ | ✗ | [7] | [2, 7, 8] | 7.9 s |
| hyb-03 | hybrid | hybrid | ✓ | [4] | [4] | 9.9 s |
| hyb-04 | hybrid | pdf ✗ | ✓ | [4] | [4, 17] | 5.4 s |
| hyb-05 | hybrid | pdf ✗ | ✓ | [17] | [17] | 7.3 s |
| hyb-06 | hybrid | pdf ✗ | ✗ | [9] | [9] | 9.3 s |
| web-01 | web | web | ✓ | - | - | 6.5 s |
| web-02 | web | web | ✓ | - | - | 6.7 s |
| web-03 | web | web | ✓ | - | - | 8.0 s |
| web-04 | web | web | ✓ | - | - | 8.5 s |
| web-05 | web | web | ✓ | - | - | 5.3 s |
| web-06 | web | web | ✓ | - | - | 8.0 s |
| web-01-webOff | none | none | ✓ | - | - | 6.5 s |
| web-02-webOff | none | none | ✓ | - | - | 8.0 s |
| web-03-webOff | none | none | ✓ | - | - | 8.7 s |
| web-04-webOff | none | none | ✓ | - | - | 2.0 s |
| web-05-webOff | none | none | ✓ | - | - | 2.2 s |
| web-06-webOff | none | none | ✓ | - | - | 8.0 s |
