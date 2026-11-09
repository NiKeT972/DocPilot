from docpilot.pdf import PageText, chunk_pages, clean_text, is_scanned


def test_hyphenated_line_breaks_are_joined():
    assert clean_text("The infor-\nmation is re-\n  levant") == "The information is relevant"


def test_whitespace_collapsed_and_paragraphs_kept():
    out = clean_text("a   b\t\tc\nd\n\n\n\ne")
    assert out == "a b c d\n\ne"


def test_control_chars_removed():
    assert clean_text("x\x00y\x07z") == "x y z"


def test_scanned_detection():
    assert is_scanned("")
    assert is_scanned("  12 \n ")
    assert not is_scanned("This page has a real text layer with words.")


def test_chunking_keeps_pages_and_indices():
    long_text = " ".join(f"Sentence number {i} talks about topic {i % 7}." for i in range(120))
    pages = [PageText(1, long_text), PageText(2, "tiny"), PageText(3, "Page three has enough text to keep.")]
    chunks = chunk_pages(pages, "doc-1", "report.pdf", chunk_size=300, chunk_overlap=50)
    assert {c.page for c in chunks} == {1, 3}, "short pages (<20 chars) are dropped"
    assert all(len(c.content) <= 300 for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.doc_id == "doc-1" and c.doc_name == "report.pdf" for c in chunks)
    page1 = [c for c in chunks if c.page == 1]
    assert len(page1) > 3
    # overlap: consecutive chunks share some text
    assert any(page1[i].content[-20:].split()[-1] in page1[i + 1].content for i in range(len(page1) - 1))


def test_chunk_start_index_continues_across_batches():
    a = chunk_pages([PageText(1, "First batch page with plenty of words.")], "d", "n", start_index=0)
    b = chunk_pages([PageText(2, "Second batch page with plenty of words.")], "d", "n", start_index=len(a))
    assert b[0].chunk_index == len(a)
