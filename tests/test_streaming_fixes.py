"""Regression tests for confirmed streaming de-anonymization fixes:
thinking_delta handling, JSON-safe tool-input reversal, multibyte-split
decoding, and flushing held-back text on a truncated stream."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custodio.streaming import deanonymize_sse_bytes, deanonymize_sse_stream  # noqa: E402


def _sse(*events):
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _collect(chunks, reverse):
    async def run():
        async def gen():
            for c in chunks:
                yield c
        out = []
        async for b in deanonymize_sse_stream(gen(), reverse):
            out.append(b)
        return b"".join(out)
    return asyncio.run(run())


def _reassemble(raw: bytes, dtype: str, key: str) -> str:
    text = ""
    for line in raw.decode().splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if d.get("type") == "content_block_delta" and d["delta"].get("type") == dtype:
                text += d["delta"][key]
    return text


def test_thinking_delta_deanonymized_across_split():
    rev = {"<PERSON_0>": "Jane Doe"}
    stream = _sse(
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta", "thinking": "user <PER"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "thinking_delta", "thinking": "SON_0> asked"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    )
    out = deanonymize_sse_bytes(stream, rev)
    assert _reassemble(out, "thinking_delta", "thinking") == "user Jane Doe asked"


def test_tool_input_json_reversal_is_escaped():
    # original value contains characters that MUST be JSON-escaped
    rev = {"<PERSON_0>": 'O"Brien\\X'}
    stream = _sse(
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": '{"n":"<PER'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta", "partial_json": 'SON_0>"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    )
    out = deanonymize_sse_bytes(stream, rev)
    partial = _reassemble(out, "input_json_delta", "partial_json")
    assert json.loads(partial) == {"n": 'O"Brien\\X'}  # valid JSON, value restored


def test_multibyte_codepoint_split_across_byte_chunks():
    rev = {"<PERSON_0>": "Jane"}
    # A text_delta carrying 'café' as RAW UTF-8 (ensure_ascii=False, as a real
    # server may send); split the raw bytes inside the 2-byte 'é'.
    d = {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "café for <PERSON_0>"}}
    raw = f"event: content_block_delta\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode()
    marker = b"caf\xc3"  # é == b"\xc3\xa9"; cut right after the lead byte
    cut = raw.index(marker) + len(marker)
    out = _collect([raw[:cut], raw[cut:]], rev)
    text = _reassemble(out, "text_delta", "text")
    assert text == "café for Jane"
    assert "�" not in out.decode()  # no replacement char from a bad split


def test_truncated_stream_flushes_held_back_tail():
    rev = {"<PERSON_0>": "Jane"}
    # delta ends on '<PER' (a placeholder prefix, so held back) and the stream
    # ends WITHOUT a content_block_stop.
    stream = _sse(
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hello <PER"}}),
    )
    out = deanonymize_sse_bytes(stream, rev)
    assert _reassemble(out, "text_delta", "text") == "Hello <PER"  # not dropped


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
