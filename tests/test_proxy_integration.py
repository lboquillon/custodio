"""End-to-end proxy test with a fake PII engine and a mock upstream.

Proves the full pipeline (anonymize request -> forward -> de-anonymize response)
for streaming and non-streaming, plus count_tokens and passthrough — WITHOUT
needing Presidio/spaCy. Requires: fastapi, httpx.

Run: python -m pytest tests/test_proxy_integration.py  (or run this file directly)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from custodio.config import Settings  # noqa: E402
from custodio.pii import EntityHit, PossibleMiss  # noqa: E402
from custodio.proxy import create_app  # noqa: E402

VALUES = {"Jane Doe": ("PERSON", "PERSON"), "jane@acme.com": ("EMAIL_ADDRESS", "EMAIL_ADDRESS")}


class FakeEngine:
    """Deterministic stand-in for PIIEngine: matches two known values."""

    def process_span(self, text, entity_mapping, reserved=None):
        hits, misses = [], []
        out = text
        for value, (etype, _) in VALUES.items():
            if value in out:
                per_type = entity_mapping.setdefault(etype, {})
                if value not in per_type:
                    per_type[value] = f"<{etype}_{len(per_type)}>"
                ph = per_type[value]
                out = out.replace(value, ph)
                hits.append(EntityHit(etype, ph, "***", 0.95))
        if "secretco" in text:  # a low-confidence "possible miss"
            misses.append(PossibleMiss("ORGANIZATION", "s***", 0.35))
        return out, hits, misses


def make_client(upstream_handler):
    settings = Settings(upstream_base_url="https://upstream.test")
    app = create_app(settings)
    tc = TestClient(app)
    tc.__enter__()  # runs lifespan (creates the real upstream client)
    core = app.state.custodio
    core._engine = FakeEngine()  # inject fake engine (skip Presidio load)
    core.client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler),
        base_url=settings.upstream_base_url,
    )
    return tc, core


def test_non_streaming_roundtrip():
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        # upstream replies referencing the placeholder it received
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"type": "message", "role": "assistant",
                  "content": [{"type": "text", "text": "Hello <PERSON_0>, I mailed <EMAIL_ADDRESS_0>."}]},
        )

    tc, core = make_client(upstream)
    try:
        r = tc.post("/v1/messages", json={
            "model": "claude-x", "max_tokens": 10,
            "system": "Help Jane Doe.",
            "messages": [{"role": "user", "content": "my email is jane@acme.com"}],
        })
        assert r.status_code == 200
        # 1) upstream never saw real PII
        upstream_text = json.dumps(seen["body"])
        assert "Jane Doe" not in upstream_text and "jane@acme.com" not in upstream_text
        assert "<PERSON_0>" in upstream_text and "<EMAIL_ADDRESS_0>" in upstream_text
        # 2) client got real PII back
        body = r.json()
        assert body["content"][0]["text"] == "Hello Jane Doe, I mailed jane@acme.com."
        # 3) audit recorded it
        events = tc.get("/custodio/events").json()
        assert events[0]["entity_count"] == 2
    finally:
        tc.__exit__(None, None, None)


def _sse(*events):
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def test_streaming_roundtrip_split_placeholder():
    def upstream(request: httpx.Request) -> httpx.Response:
        stream = _sse(
            ("message_start", {"type": "message_start", "message": {"id": "m1"}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "Hi <PER"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "SON_0>!"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    tc, core = make_client(upstream)
    try:
        with tc.stream("POST", "/v1/messages", json={
            "model": "claude-x", "stream": True, "max_tokens": 10,
            "messages": [{"role": "user", "content": "I am Jane Doe"}],
        }) as r:
            raw = b"".join(r.iter_bytes()).decode()
        text = ""
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if d.get("type") == "content_block_delta" and d["delta"].get("type") == "text_delta":
                    text += d["delta"]["text"]
        assert text == "Hi Jane Doe!"  # re-assembled + de-anonymized across the split
    finally:
        tc.__exit__(None, None, None)


def test_count_tokens_anonymized_not_deanonymized():
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={"input_tokens": 42})

    tc, core = make_client(upstream)
    try:
        r = tc.post("/v1/messages/count_tokens", json={
            "model": "claude-x",
            "messages": [{"role": "user", "content": "Jane Doe here"}],
        })
        assert r.status_code == 200
        assert r.json() == {"input_tokens": 42}
        assert "Jane Doe" not in json.dumps(seen["body"])  # still anonymized upstream
    finally:
        tc.__exit__(None, None, None)


def test_passthrough_untouched():
    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": ["claude-x"]})

    tc, core = make_client(upstream)
    try:
        r = tc.get("/v1/models")
        assert r.status_code == 200 and r.json() == {"data": ["claude-x"]}
    finally:
        tc.__exit__(None, None, None)


def test_possible_miss_surfaced():
    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={"type": "message", "content": [{"type": "text", "text": "ok"}]})

    tc, core = make_client(upstream)
    try:
        tc.post("/v1/messages", json={
            "model": "claude-x", "max_tokens": 5,
            "messages": [{"role": "user", "content": "I work at secretco"}],
        })
        ev = tc.get("/custodio/events").json()[0]
        assert ev["possible_miss_count"] == 1
    finally:
        tc.__exit__(None, None, None)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
