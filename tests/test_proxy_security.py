"""Regression tests for proxy-level leak/DoS fixes:
fail-closed on un-parseable/encoded bodies, request-size cap, content-encoding
stripping, and the live SSE endpoint."""

import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from custodio.config import Settings  # noqa: E402
from custodio.pii import EntityHit  # noqa: E402
from custodio.proxy import _request_headers, create_app  # noqa: E402


class FakeEngine:
    def process_span(self, text, entity_mapping, reserved=None):
        hits = []
        out = text
        if "Jane Doe" in out:
            per = entity_mapping.setdefault("PERSON", {})
            per.setdefault("Jane Doe", "<PERSON_0>")
            out = out.replace("Jane Doe", "<PERSON_0>")
            hits.append(EntityHit("PERSON", "<PERSON_0>", "***", 0.95))
        return out, hits, []


def make_client(upstream_handler, settings=None):
    settings = settings or Settings(upstream_base_url="https://upstream.test")
    app = create_app(settings)
    tc = TestClient(app)
    tc.__enter__()
    core = app.state.custodio
    core._engine = FakeEngine()
    core.client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream_handler),
        base_url=settings.upstream_base_url,
    )
    return tc, core


def test_gzip_encoded_body_fails_closed():
    called = {"n": 0}

    def upstream(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    tc, _ = make_client(upstream)
    try:
        body = gzip.compress(json.dumps(
            {"model": "m", "messages": [{"role": "user", "content": "Jane Doe SSN"}]}
        ).encode())
        r = tc.post("/v1/messages", content=body,
                    headers={"content-type": "application/json", "content-encoding": "gzip"})
        assert r.status_code == 415
        assert called["n"] == 0  # nothing forwarded upstream
    finally:
        tc.__exit__(None, None, None)


def test_non_json_body_fails_closed():
    called = {"n": 0}

    def upstream(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    tc, _ = make_client(upstream)
    try:
        r = tc.post("/v1/messages", content=b"this is not json",
                    headers={"content-type": "application/json"})
        assert r.status_code == 415
        assert called["n"] == 0
    finally:
        tc.__exit__(None, None, None)


def test_non_json_body_passthrough_when_fail_open():
    seen = {}

    def upstream(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    tc, _ = make_client(upstream, Settings(upstream_base_url="https://upstream.test", fail_open=True))
    try:
        r = tc.post("/v1/messages", content=b"raw passthrough",
                    headers={"content-type": "application/json"})
        assert r.status_code == 200
        assert seen["body"] == b"raw passthrough"
    finally:
        tc.__exit__(None, None, None)


def test_oversized_body_rejected_413():
    def upstream(request):
        return httpx.Response(200, json={})

    tc, _ = make_client(upstream, Settings(upstream_base_url="https://upstream.test", max_body_bytes=64))
    try:
        big = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "x" * 500}]})
        r = tc.post("/v1/messages", content=big,
                    headers={"content-type": "application/json"})
        assert r.status_code == 413
    finally:
        tc.__exit__(None, None, None)


def test_request_headers_strip_content_encoding():
    hdrs = httpx.Headers({"content-encoding": "gzip", "authorization": "Bearer x",
                          "anthropic-beta": "oauth", "host": "h"})
    out = {k.lower() for k in _request_headers(hdrs)}
    assert "content-encoding" not in out and "host" not in out
    assert "authorization" in out and "anthropic-beta" in out  # auth preserved


def test_sse_stream_endpoint_serves_event_stream():
    """Drive the ASGI endpoint directly: an in-process HTTP client would try to
    buffer the (infinite) stream and never return, so we speak ASGI and send a
    disconnect after the first body chunk."""
    import asyncio

    async def run():
        app = create_app(Settings())
        scope = {"type": "http", "method": "GET", "path": "/custodio/stream",
                 "headers": [], "query_string": b""}
        sent = []
        disconnect = asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            sent.append(msg)
            if msg["type"] == "http.response.body" and msg.get("body"):
                disconnect.set()  # got first chunk -> hang up

        task = asyncio.create_task(app(scope, receive, send))
        for _ in range(200):
            if any(m["type"] == "http.response.body" and m.get("body") for m in sent):
                break
            await asyncio.sleep(0.01)
        disconnect.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
        return sent

    sent = asyncio.run(run())
    start = next(m for m in sent if m["type"] == "http.response.start")
    ctype = dict((k.decode(), v.decode()) for k, v in start["headers"]).get("content-type", "")
    assert start["status"] == 200 and ctype.startswith("text/event-stream")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"retry:" in body


def test_audit_token_guards_endpoints():
    def upstream(request):
        return httpx.Response(200, json={})

    tc, _ = make_client(upstream, Settings(upstream_base_url="https://upstream.test",
                                           audit_token="s3cret"))
    try:
        # no token -> 401 on protected endpoints
        assert tc.get("/custodio/events").status_code == 401
        assert tc.get("/custodio/stats").status_code == 401
        assert tc.get("/custodio/dashboard").status_code == 401
        # correct token via header, query, OR cookie -> 200
        assert tc.get("/custodio/events", headers={"authorization": "Bearer s3cret"}).status_code == 200
        assert tc.get("/custodio/events?token=s3cret").status_code == 200
        assert tc.get("/custodio/events", headers={"cookie": "custodio_token=s3cret"}).status_code == 200
        # wrong token -> 401
        assert tc.get("/custodio/events?token=nope").status_code == 401
        # health stays public but returns only {status} without a token...
        h = tc.get("/custodio/health")
        assert h.status_code == 200 and set(h.json()) == {"status"}
        # ...and reveals details only when authorized
        assert "upstream" in tc.get("/custodio/health?token=s3cret").json()
        # assets stay public (fonts)
        assert tc.get("/custodio/assets/space-mono-400.woff2").status_code == 200
    finally:
        tc.__exit__(None, None, None)


def test_no_token_means_open():
    def upstream(request):
        return httpx.Response(200, json={})

    tc, _ = make_client(upstream)  # default: no audit_token
    try:
        assert tc.get("/custodio/events").status_code == 200
        assert tc.get("/custodio/dashboard").status_code == 200
    finally:
        tc.__exit__(None, None, None)


def test_font_asset_served_and_unknown_rejected():
    def upstream(request):
        return httpx.Response(200, json={})

    tc, _ = make_client(upstream)
    try:
        r = tc.get("/custodio/assets/space-mono-400.woff2")
        assert r.status_code == 200
        assert r.headers["content-type"] == "font/woff2"
        assert r.content[:4] == b"wOF2"  # valid woff2 magic
        assert "immutable" in r.headers.get("cache-control", "")
        # only whitelisted files; anything else is 404 (no traversal/leak)
        assert tc.get("/custodio/assets/proxy.py").status_code == 404
        assert tc.get("/custodio/assets/unknown.woff2").status_code == 404
    finally:
        tc.__exit__(None, None, None)


def test_end_to_end_request_publishes_to_bus():
    """A processed request lands on the live bus (what the SSE feed pushes)."""
    def upstream(request):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={"type": "message", "content": [{"type": "text", "text": "ok"}]})

    tc, core = make_client(upstream)
    try:
        q = core.bus.subscribe()
        tc.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                "messages": [{"role": "user", "content": "I am Jane Doe"}]})
        # add() + finalize() each publish; drain and check the newest.
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        assert msgs and msgs[-1]["event"]["entity_count"] == 1
        assert msgs[-1]["event"]["status"] == 200
    finally:
        tc.__exit__(None, None, None)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
