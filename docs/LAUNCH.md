# DocPilot launch kit

## Tomorrow's checklist (≈20 min of clicks)

1. **Gemini billing**: aistudio.google.com/apikey → your key shows **Tier 1** (not "Free"). Tell Claude "billing on".
2. **Vercel env vars**: vercel.com → project **docpilot** → Settings → Environment Variables. Add each one for **Production and Preview**:

   | Key | Value | Type |
   |---|---|---|
   | `GROQ_API_KEY` | your `gsk_…` key | Sensitive |
   | `GEMINI_API_KEY` | your `AIza…` key | Sensitive |
   | `TAVILY_API_KEY` | your `tvly-…` key | Sensitive |
   | `SUPABASE_URL` | `https://fsgxdrtscovxrwhmettd.supabase.co` | Plain |
   | `SUPABASE_SERVICE_KEY` | the Supabase key that works (the one now in the session's network secrets) | Sensitive |
   | `IP_HASH_SALT` | any long random string (e.g. from a password generator, 40+ chars) | Sensitive |
   | `CRON_SECRET` | another long random string | Sensitive |

   The model names have defaults in code, so you don't need to add them.
3. **Deploy**: Deployments → ⋯ → **Redeploy** (or Claude pushes a commit). The production URL is `docpilot-….vercel.app`; you can rename it under Settings → Domains.
4. Claude then runs the eval, smoke-tests the live URL, and fills in the README link and numbers.

## Demo GIF script (≈40 s; record at 1440×900 with ScreenToGif / Kap / Loom)

1. **0–4 s**: Empty state. Hover over the 4-step flow (Search PDFs → Grade → Web if needed → Cited answer).
2. **4–12 s**: Drag in a 100-page PDF. Show the chip go *Reading 40/100 → Indexing 60% → Ready*, then the 3 suggested-question chips appear.
3. **12–20 s**: Click a suggestion. The live timeline ticks through *Searching 1 document… → Checking relevance: 4/6 relevant*, then the answer streams with green `P` pills and the **From your PDFs** badge.
4. **20–27 s**: Hover a `P` pill (snippet tooltip), click it. The viewer slides in at the cited page with the passage highlighted.
5. **27–36 s**: Turn on *Search the web*, ask something half in the PDF ("What does the report say about X, and what has happened since?"). Amber `W` pills, **PDFs + web** badge, source cards with favicons.
6. **36–40 s**: Expand *How I got this answer* to show per-step timings. End on the full screen.

Tip: speed the indexing part up 2–3× in the editor and keep the cursor movements slow.

## Resume bullets (fill the [brackets] from `eval/results.md`)

- Built **DocPilot**, an agentic PDF Q&A app (LangGraph corrective RAG, Groq gpt-oss-120b, Gemini embeddings, Supabase pgvector, FastAPI on Vercel). It grades its own retrieval, falls back to labelled web search, and streams answers with clickable page citations. On a 30-question benchmark it scored **[X]% answer correctness**, **[Y]% routing accuracy** and **[Z]% citation page accuracy** at **[p50] s median latency**.
- Engineered hybrid retrieval (HNSW cosine + Postgres full-text fused with Reciprocal Rank Fusion in one SQL function) and a serverless ingestion pipeline. It parses 1,000-page PDFs in the browser, OCRs scanned pages with Gemini vision, and is covered by 80 offline tests plus a Playwright E2E. It also includes prompt-injection hardening, per-IP rate limits and CI.

## LinkedIn post (3 lines)

> Shipped **DocPilot**: upload PDFs, ask questions, get answers with **clickable page citations** that open the exact passage. When the PDFs don't have it, it searches the web and clearly labels that part.
> Under the hood: a LangGraph corrective-RAG agent (retrieve → grade → web fallback → verify citations), hybrid pgvector + full-text search with RRF, streaming FastAPI on Vercel. [X]% answer correctness on a 30-question eval.
> Live demo + code: [link] · Built with Groq, Gemini, Supabase and Tavily. Feedback welcome!
