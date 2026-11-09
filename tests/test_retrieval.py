import pytest
from fakes import add_doc, make_services

from docpilot.store import MemoryStore, rrf_merge, tokenize

SESSION = "retrieval-session"


def test_rrf_merge_scores_and_order():
    vec = ["a", "b", "c"]
    fts = ["c", "a", "d"]
    merged = dict(rrf_merge([vec, fts], k=60))
    assert merged["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert merged["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert merged["b"] == pytest.approx(1 / 62)
    assert merged["d"] == pytest.approx(1 / 63)
    order = [k for k, _ in rrf_merge([vec, fts], k=60)]
    assert order == ["a", "c", "b", "d"]


def test_rrf_items_in_both_lists_beat_single_list_top():
    order = [k for k, _ in rrf_merge([["x", "y"], ["z", "y"]])]
    assert order[0] == "y"


def test_rrf_ties_broken_by_first_seen():
    assert [k for k, _ in rrf_merge([["a"], ["b"]])] == ["a", "b"]


def test_tokenize_drops_stopwords():
    assert tokenize("What is the Repo Rate in 2024?") == ["repo", "rate", "2024"]


async def test_hybrid_search_finds_exact_keyword_and_respects_scope():
    svc = make_services(store=MemoryStore())
    d1 = await add_doc(
        svc,
        SESSION,
        "a.pdf",
        ["General discussion of monetary policy and inflation targets."] * 3
        + ["The code XQ-4471 identifies the special liquidity facility."],
    )
    d2 = await add_doc(svc, SESSION, "b.pdf", ["Transformers use self-attention over tokens."])
    await add_doc(svc, "other-session-1", "c.pdf", ["XQ-4471 also appears in another session."])

    q = "What is XQ-4471?"
    emb = await svc.embedder.embed_query(q)
    res = await svc.store.search(SESSION, q, emb, None, k=6, candidate_k=8)
    assert res and res[0].page == 4 and res[0].doc_id == d1.id
    assert all(r.doc_id in {d1.id, d2.id} for r in res), "never leaks other sessions"

    only_b = await svc.store.search(SESSION, q, emb, [d2.id], k=6, candidate_k=8)
    assert {r.doc_id for r in only_b} == {d2.id}


async def test_delete_doc_removes_chunks():
    svc = make_services()
    d1 = await add_doc(svc, SESSION, "a.pdf", ["Alpha beta gamma delta epsilon."])
    assert await svc.store.delete_doc(SESSION, d1.id)
    assert not await svc.store.delete_doc(SESSION, d1.id)
    emb = await svc.embedder.embed_query("alpha")
    assert await svc.store.search(SESSION, "alpha", emb, None, 6, 8) == []


async def test_delete_doc_requires_matching_session():
    svc = make_services()
    d1 = await add_doc(svc, SESSION, "a.pdf", ["Alpha beta gamma delta epsilon."])
    assert not await svc.store.delete_doc("someone-else-123", d1.id)
    assert await svc.store.get_doc(SESSION, d1.id)


def test_supabase_headers_for_new_and_legacy_keys():
    from docpilot.store import SupabaseStore

    new = SupabaseStore("https://x.supabase.co", "sb_secret_abc").headers
    assert new["apikey"] == "sb_secret_abc" and "Authorization" not in new
    legacy = SupabaseStore("https://x.supabase.co/", "eyJhbGciOi.legacy").headers
    assert legacy["Authorization"] == "Bearer eyJhbGciOi.legacy"
