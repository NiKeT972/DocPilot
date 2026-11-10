# DocPilot evaluation results

Run: 2026-11-10 12:42 UTC · server `http://localhost:8000` · store `supabase` · answer `openai/gpt-oss-120b` · grader/rewriter `openai/gpt-oss-20b` · embeddings `gemini-embedding-001` (768d) · web search Tavily

Documents: *Attention Is All You Need* (arXiv:1706.03762, 15 pages) and *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks* (arXiv:2005.11401, 19 pages), uploaded through the public API.

## Summary

| Metric | Result |
|---|---|
| Routing accuracy (expected vs actual route) | **83%** (30/36) |
| Answer correctness (LLM-as-judge, strict 0/1) | **89%** (32/36) |
| Citation page accuracy (a cited page holds the answer) | **100%** (22/22 answers with PDF citations) |
| Latency p50 / p95 (end-to-end, client-measured) | **8.2 s / 12.3 s** |

## By category

| Category | n | Routing | Correct | p50 latency |
|---|---|---|---|---|
| Answerable from PDFs | 18 | 94% | 94% | 7.0 s |
| Needs PDF + web | 6 | 33% | 67% | 9.2 s |
| Not in PDFs (web on) | 6 | 83% | 83% | 8.5 s |
| Not in PDFs (web off -> expect 'none') | 6 | 100% | 100% | 8.3 s |

Route confusion (expected -> actual): `hybrid->hybrid` x2, `hybrid->none` x1, `hybrid->pdf` x3, `none->none` x6, `pdf->none` x1, `pdf->pdf` x17, `web->error` x1, `web->web` x5

## Method

- 30 questions written before any run (`eval/questions.jsonl`): 18 answerable from the PDFs with hand-verified gold pages, 6 needing PDF + web, 6 current-events questions not in the PDFs. The 6 current-events questions are asked again with web search off, expecting a 'not found' answer (36 runs total).
- Correctness is judged by the answer model with a strict rubric against the expected answer; for fast-changing current events without a fixed answer, the judge checks the answer against the cited web sources. LLM judges are imperfect; per-question reasons are in `results.json`.
- Citation page accuracy counts answers (PDF and hybrid questions) whose PDF citations include at least one gold page.
- Latency is wall-clock time for the full non-streamed JSON request from the eval client, sequential, no caching. In the streaming UI the first tokens arrive earlier.
- No prompts, thresholds or questions were tuned on these questions.

## Per question

| id | expected | actual | correct | cited pages | gold pages | latency |
|---|---|---|---|---|---|---|
| pdf-01 | pdf | pdf | ✓ | [8, 1] | [1, 8] | 4.0 s |
| pdf-02 | pdf | pdf | ✓ | [1] | [1, 8] | 3.0 s |
| pdf-03 | pdf | pdf | ✓ | [3] | [3] | 2.5 s |
| pdf-04 | pdf | pdf | ✓ | [3] | [3] | 3.1 s |
| pdf-05 | pdf | pdf | ✓ | [5] | [5] | 3.4 s |
| pdf-06 | pdf | none ✗ | ✓ | - | [4] | 4.8 s |
| pdf-07 | pdf | pdf | ✓ | [6] | [6] | 2.4 s |
| pdf-08 | pdf | pdf | ✓ | [7] | [7] | 2.7 s |
| pdf-09 | pdf | pdf | ✓ | [8] | [8] | 12.1 s |
| pdf-10 | pdf | pdf | ✓ | [8] | [8] | 10.2 s |
| pdf-11 | pdf | pdf | ✗ | [10] | [10] | 10.3 s |
| pdf-12 | pdf | pdf | ✓ | [8] | [8] | 11.2 s |
| pdf-13 | pdf | pdf | ✓ | [4] | [4] | 5.9 s |
| pdf-14 | pdf | pdf | ✓ | [3] | [3] | 10.3 s |
| pdf-15 | pdf | pdf | ✓ | [3] | [3] | 8.3 s |
| pdf-16 | pdf | pdf | ✓ | [6] | [6] | 14.6 s |
| pdf-17 | pdf | pdf | ✓ | [4] | [4, 8] | 10.3 s |
| pdf-18 | pdf | pdf | ✓ | [17] | [17] | 8.0 s |
| hyb-01 | hybrid | hybrid | ✓ | [1] | [1] | 12.0 s |
| hyb-02 | hybrid | pdf ✗ | ✗ | [7] | [2, 7, 8] | 10.2 s |
| hyb-03 | hybrid | hybrid | ✓ | [4] | [4] | 8.1 s |
| hyb-04 | hybrid | none ✗ | ✓ | - | [4, 17] | 10.2 s |
| hyb-05 | hybrid | pdf ✗ | ✓ | [17] | [17] | 7.5 s |
| hyb-06 | hybrid | pdf ✗ | ✗ | [9] | [9] | 6.3 s |
| web-01 | web | web | ✓ | - | - | 8.2 s |
| web-02 | web | web | ✓ | - | - | 12.8 s |
| web-03 | web | web | ✓ | - | - | 10.4 s |
| web-04 | web | error ✗ | ✗ | - | - | 5.4 s |
| web-05 | web | web | ✓ | - | - | 6.9 s |
| web-06 | web | web | ✓ | - | - | 8.9 s |
| web-01-webOff | none | none | ✓ | - | - | 6.4 s |
| web-02-webOff | none | none | ✓ | - | - | 8.2 s |
| web-03-webOff | none | none | ✓ | - | - | 8.4 s |
| web-04-webOff | none | none | ✓ | - | - | 9.8 s |
| web-05-webOff | none | none | ✓ | - | - | 5.5 s |
| web-06-webOff | none | none | ✓ | - | - | 8.8 s |
