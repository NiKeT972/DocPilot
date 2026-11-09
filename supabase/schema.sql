-- DocPilot schema v2 (idempotent: safe to run more than once).
-- Paste into the Supabase SQL Editor, or run with `supabase db push`/psql.
-- Requires pgvector >= 0.8 (Supabase default) for HNSW iterative scans.

create extension if not exists vector with schema extensions;
create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------- documents
create table if not exists public.documents (
  id          uuid primary key default gen_random_uuid(),
  session_id  text not null check (session_id ~ '^[A-Za-z0-9-]{8,64}$'),
  name        text not null,
  size_bytes  bigint not null default 0,
  sha256      text not null default '',
  pages       int not null default 0,
  chunks      int not null default 0,
  ocr_pages   int not null default 0,
  created_at  timestamptz not null default now()
);
create index if not exists documents_session_idx on public.documents (session_id);
create index if not exists documents_created_idx on public.documents (created_at);

-- ---------------------------------------------------------------- doc_chunks
create table if not exists public.doc_chunks (
  id           bigint generated always as identity primary key,
  session_id   text not null,
  doc_id       uuid not null references public.documents (id) on delete cascade,
  page         int not null,
  chunk_index  int not null,
  content      text not null,
  embedding    extensions.vector(768) not null,
  fts          tsvector generated always as (to_tsvector('english', content)) stored,
  created_at   timestamptz not null default now()
);
create index if not exists doc_chunks_session_idx on public.doc_chunks (session_id);
create index if not exists doc_chunks_doc_idx on public.doc_chunks (doc_id);
create index if not exists doc_chunks_created_idx on public.doc_chunks (created_at);
create index if not exists doc_chunks_embedding_hnsw
  on public.doc_chunks using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists doc_chunks_fts_gin on public.doc_chunks using gin (fts);

-- ---------------------------------------------------------------- usage_events
create table if not exists public.usage_events (
  id          bigint generated always as identity primary key,
  ip_hash     text not null,
  session_id  text,
  kind        text not null,
  created_at  timestamptz not null default now()
);
create index if not exists usage_events_lookup_idx on public.usage_events (ip_hash, kind, created_at);

-- ---------------------------------------------------------------- RLS
-- Only the server (service-role key, which bypasses RLS) touches these tables.
-- RLS on with no policies = anon/authenticated keys get nothing.
alter table public.documents    enable row level security;
alter table public.doc_chunks   enable row level security;
alter table public.usage_events enable row level security;
revoke all on public.documents, public.doc_chunks, public.usage_events from anon, authenticated;

-- ---------------------------------------------------------------- RPCs
-- Hybrid retrieval: vector top-k (cosine) + full-text top-k, merged with
-- Reciprocal Rank Fusion. Scoped to a session and optionally to some docs.
create or replace function public.match_chunks_hybrid(
  query_embedding extensions.vector(768),
  query_text      text,
  p_session_id    text,
  p_doc_ids       uuid[] default null,
  match_count     int default 6,
  candidate_count int default 8,
  rrf_k           int default 60
)
returns table (
  id          bigint,
  doc_id      uuid,
  doc_name    text,
  page        int,
  chunk_index int,
  content     text,
  similarity  float,
  rank        float
)
language sql stable
set search_path = public, extensions
set hnsw.iterative_scan = relaxed_order
as $$
  with vec as (
    select c.id,
           row_number() over (order by c.embedding <=> query_embedding) as r
    from doc_chunks c
    where c.session_id = p_session_id
      and (p_doc_ids is null or c.doc_id = any (p_doc_ids))
    order by c.embedding <=> query_embedding
    limit candidate_count
  ),
  q as (select websearch_to_tsquery('english', coalesce(query_text, '')) as tsq),
  fts as (
    select c.id,
           row_number() over (order by ts_rank_cd(c.fts, q.tsq) desc) as r
    from doc_chunks c, q
    where c.session_id = p_session_id
      and (p_doc_ids is null or c.doc_id = any (p_doc_ids))
      and c.fts @@ q.tsq
    order by ts_rank_cd(c.fts, q.tsq) desc
    limit candidate_count
  ),
  merged as (
    select coalesce(vec.id, fts.id) as id,
           coalesce(1.0 / (rrf_k + vec.r), 0.0) + coalesce(1.0 / (rrf_k + fts.r), 0.0) as rank
    from vec full outer join fts on vec.id = fts.id
  )
  select c.id, c.doc_id, d.name as doc_name, c.page, c.chunk_index, c.content,
         (1 - (c.embedding <=> query_embedding))::float as similarity,
         m.rank::float as rank
  from merged m
  join doc_chunks c on c.id = m.id
  join documents d on d.id = c.doc_id
  order by m.rank desc, c.id
  limit match_count;
$$;

create or replace function public.list_session_docs(p_session_id text)
returns setof public.documents
language sql stable
set search_path = public
as $$
  select * from documents where session_id = p_session_id order by created_at;
$$;

-- Atomically bump a document's counters after a batch is ingested.
create or replace function public.bump_doc_counts(
  p_doc_id uuid, p_session_id text, p_chunks int, p_ocr_pages int
)
returns public.documents
language sql
set search_path = public
as $$
  update documents
     set chunks = chunks + p_chunks,
         ocr_pages = ocr_pages + p_ocr_pages
   where id = p_doc_id and session_id = p_session_id
  returning *;
$$;

create or replace function public.count_usage(p_ip_hash text, p_kind text, p_since timestamptz)
returns bigint
language sql stable
set search_path = public
as $$
  select count(*) from usage_events
   where ip_hash = p_ip_hash and kind = p_kind and created_at >= p_since;
$$;

-- Retention: remove everything older than p_days (called by the Vercel Cron).
create or replace function public.cleanup_old_data(p_days int default 7)
returns json
language plpgsql
set search_path = public
as $$
declare
  n_docs bigint;
  n_events bigint;
begin
  delete from documents where created_at < now() - make_interval(days => p_days);
  get diagnostics n_docs = row_count;
  delete from usage_events where created_at < now() - make_interval(days => greatest(p_days, 2));
  get diagnostics n_events = row_count;
  return json_build_object('documents', n_docs, 'usage_events', n_events);
end;
$$;

revoke execute on function public.match_chunks_hybrid(extensions.vector, text, text, uuid[], int, int, int) from anon, authenticated, public;
revoke execute on function public.list_session_docs(text) from anon, authenticated, public;
revoke execute on function public.bump_doc_counts(uuid, text, int, int) from anon, authenticated, public;
revoke execute on function public.count_usage(text, text, timestamptz) from anon, authenticated, public;
revoke execute on function public.cleanup_old_data(int) from anon, authenticated, public;
grant execute on function public.match_chunks_hybrid(extensions.vector, text, text, uuid[], int, int, int) to service_role;
grant execute on function public.list_session_docs(text) to service_role;
grant execute on function public.bump_doc_counts(uuid, text, int, int) to service_role;
grant execute on function public.count_usage(text, text, timestamptz) to service_role;
grant execute on function public.cleanup_old_data(int) to service_role;
