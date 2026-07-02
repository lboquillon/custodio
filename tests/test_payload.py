"""Tests for the Anthropic payload walkers (no Presidio needed)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custodio.anthropic_payload import (  # noqa: E402
    anonymize_request,
    deanonymize_response,
)
from custodio.config import Settings  # noqa: E402
from custodio.pii import EntityHit  # noqa: E402


def fake_anon(text):
    """Toy anonymizer: Jane Doe -> <PERSON_0>, jane@acme.com -> <EMAIL_ADDRESS_0>."""
    mapping = {"Jane Doe": "<PERSON_0>", "jane@acme.com": "<EMAIL_ADDRESS_0>"}
    hits = []
    out = text
    for original, ph in mapping.items():
        if original in out:
            out = out.replace(original, ph)
            etype = "PERSON" if "PERSON" in ph else "EMAIL_ADDRESS"
            hits.append(EntityHit(etype, ph, "***", 0.9))
    return out, hits


def fake_deanon(text):
    rev = {"<PERSON_0>": "Jane Doe", "<EMAIL_ADDRESS_0>": "jane@acme.com"}
    for ph, orig in rev.items():
        text = text.replace(ph, orig)
    return text


S = Settings()


def test_string_system_and_content():
    payload = {
        "model": "claude",
        "system": "You help Jane Doe.",
        "messages": [{"role": "user", "content": "Email jane@acme.com please"}],
    }
    hits = anonymize_request(payload, fake_anon, S)
    assert payload["system"] == "You help <PERSON_0>."
    assert payload["messages"][0]["content"] == "Email <EMAIL_ADDRESS_0> please"
    assert len(hits) == 2


def test_block_content_and_tool_result():
    payload = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "hi Jane Doe"},
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "output for jane@acme.com"},
            ]},
        ]
    }
    anonymize_request(payload, fake_anon, S)
    blocks = payload["messages"][0]["content"]
    assert blocks[0]["text"] == "hi <PERSON_0>"
    assert blocks[1]["content"] == "output for <EMAIL_ADDRESS_0>"


def test_tool_use_input_string_leaves():
    payload = {
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read",
                 "input": {"path": "/home/Jane Doe/x", "n": 3}},
            ]},
        ]
    }
    anonymize_request(payload, fake_anon, S)
    inp = payload["messages"][0]["content"][0]["input"]
    assert inp["path"] == "/home/<PERSON_0>/x"
    assert inp["n"] == 3  # non-strings untouched


def test_tool_defs_skipped_by_default():
    payload = {"messages": [], "tools": [{"name": "x", "description": "for Jane Doe"}]}
    anonymize_request(payload, fake_anon, S)
    assert payload["tools"][0]["description"] == "for Jane Doe"


def test_tool_defs_included_when_enabled():
    s = Settings(anonymize_tool_defs=True)
    payload = {"messages": [], "tools": [{"name": "x", "description": "for Jane Doe"}]}
    anonymize_request(payload, fake_anon, s)
    assert payload["tools"][0]["description"] == "for <PERSON_0>"


def test_deanonymize_response_text_and_tool_use():
    resp = {
        "content": [
            {"type": "text", "text": "Hi <PERSON_0>"},
            {"type": "tool_use", "input": {"to": "<EMAIL_ADDRESS_0>"}},
        ]
    }
    deanonymize_response(resp, fake_deanon)
    assert resp["content"][0]["text"] == "Hi Jane Doe"
    assert resp["content"][1]["input"]["to"] == "jane@acme.com"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
