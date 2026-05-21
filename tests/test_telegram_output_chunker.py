from wlcodex.telegram_output import ChunkPolicy, SemanticChunker


def test_chunker_prefers_paragraph_boundary():
    chunker = SemanticChunker(ChunkPolicy(min_chars=10, max_chars=60))
    chunker.append("第一段很长很长。\n\n第二段也很长很长。")

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["第一段很长很长。"]
    assert chunker.buffer == "第二段也很长很长。"


def test_chunker_does_not_split_markdown_link_when_avoidable():
    chunker = SemanticChunker(ChunkPolicy(min_chars=20, max_chars=80))
    text = "来源：[上海黄金交易所](https://www.sge.com.cn/sjzx/yshqbg)\n\n下一段。"
    chunker.append(text)

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["来源：[上海黄金交易所](https://www.sge.com.cn/sjzx/yshqbg)"]
    assert ".cn/)" not in chunks


def test_chunker_keeps_list_item_readable():
    chunker = SemanticChunker(ChunkPolicy(min_chars=10, max_chars=25))
    chunker.append("- 国内金价：986 元/克\n- 周大福首饰金：1396 元/克\n- 回收价：971 元/克")

    chunks = chunker.ready_chunks(force=False)

    # Every chunk must start with "- " (never split mid-item)
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.startswith("- ")
    # Remaining buffer is also valid
    assert chunker.buffer.startswith("- ") or chunker.buffer == ""


def test_chunker_does_not_split_inside_code_fence_when_avoidable():
    chunker = SemanticChunker(ChunkPolicy(min_chars=20, max_chars=120))
    chunker.append("说明：\n\n```bash\npytest tests/test_streaming.py -q\n```\n\n结束。")

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["说明：\n\n```bash\npytest tests/test_streaming.py -q\n```"]
    assert chunker.buffer == "结束。"


def test_chunker_flushes_final_chunks_with_part_numbers():
    chunker = SemanticChunker(ChunkPolicy(min_chars=10, max_chars=30, final_max_chars=25))
    # Build text that exceeds final_max_chars so it must split
    chunker.append("第一句很长很长很长。第二句很长很长很长。第三句很长很长很长。")

    chunks = chunker.final_chunks(number_parts=True)

    assert len(chunks) >= 2
    assert chunks[0].startswith("1/")
    assert chunks[-1].startswith(f"{len(chunks)}/")


def test_chunker_hard_split_keeps_each_code_fence_chunk_balanced():
    chunker = SemanticChunker(ChunkPolicy(min_chars=1, max_chars=40, final_max_chars=40))
    chunker.append("```python\n" + ("x = 1\n" * 20) + "```")

    chunks = chunker.final_chunks(number_parts=True)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_chunker_prefers_whitespace_before_hard_split():
    chunker = SemanticChunker(ChunkPolicy(min_chars=10, max_chars=24))
    chunker.append("alpha beta gamma delta epsilon")

    chunks = chunker.ready_chunks(force=False)

    assert chunks == ["alpha beta gamma delta"]
    assert chunker.buffer == "epsilon"
