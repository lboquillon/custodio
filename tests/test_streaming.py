"""Tests for the placeholder-safe streaming de-anonymizer.

Run: python -m pytest tests/test_streaming.py   (or `python tests/test_streaming.py`)
These need no Presidio / FastAPI.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custodio.streaming import (  # noqa: E402
    PlaceholderStreamDeanonymizer,
    deanonymize_sse_bytes,
)

REVERSE = {"<PERSON_0>": "Jane Doe", "<EMAIL_ADDRESS_0>": "jane@acme.com"}


def _run(chunks, reverse=REVERSE):
    d = PlaceholderStreamDeanonymizer(reverse)
    out = "".join(d.feed(c) for c in chunks)
    return out + d.flush()


def test_no_placeholders_is_identity():
    assert _run(["hello ", "world"]) == "hello world"


def test_whole_placeholder_in_one_chunk():
    assert _run(["hi <PERSON_0>!"]) == "hi Jane Doe!"


def test_placeholder_split_across_chunks():
    # the hard case: token is torn in half
    assert _run(["hi <PER", "SON_0>, hello"]) == "hi Jane Doe, hello"


def test_placeholder_split_char_by_char():
    chunks = list("prefix <PERSON_0> suffix")
    assert _run(chunks) == "prefix Jane Doe suffix"


def test_two_placeholders_adjacent():
    assert _run(["<PERSON_0> <EMAIL", "_ADDRESS_0>"]) == "Jane Doe jane@acme.com"


def test_literal_angle_bracket_not_held_forever():
    # "a < b" must eventually flush; '< ' is not a placeholder prefix
    assert _run(["a < b is math"]) == "a < b is math"


def test_unclosed_literal_flushes_on_end():
    assert _run(["trailing <"]) == "trailing <"


def test_longest_match_wins():
    rev = {"<PERSON_1>": "Bob", "<PERSON_10>": "Alice"}
    assert _run(["<PERSON_10> and <PERSON_1>"], rev) == "Alice and Bob"


def _sse(*events):
    parts = []
    for name, data in events:
        parts.append(f"event: {name}\ndata: {json.dumps(data)}\n\n")
    return "".join(parts).encode()


def test_full_sse_stream_roundtrip():
    stream = _sse(
        ("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hi <PER"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "SON_0>!"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_stop", {"type": "message_stop"}),
    )
    out = deanonymize_sse_bytes(stream, REVERSE).decode()
    # reassemble the assistant text from the emitted deltas
    text = ""
    for line in out.splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if d.get("type") == "content_block_delta" and d["delta"].get("type") == "text_delta":
                text += d["delta"]["text"]
    assert text == "Hi Jane Doe!"


def test_sse_tool_input_json_delta():
    stream = _sse(
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "t1",
                                                   "name": "read", "input": {}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": '{"path":"/u/<PER'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": 'SON_0>/x"}'}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    )
    out = deanonymize_sse_bytes(stream, REVERSE).decode()
    partial = ""
    for line in out.splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if d.get("type") == "content_block_delta" and d["delta"].get("type") == "input_json_delta":
                partial += d["delta"]["partial_json"]
    assert json.loads(partial) == {"path": "/u/Jane Doe/x"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
