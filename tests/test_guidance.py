"""Tests for the placeholder-protocol guidance and default entity denylist."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from custodio.anthropic_payload import (  # noqa: E402
    PLACEHOLDER_GUIDANCE,
    inject_guidance,
)
from custodio.config import DEFAULT_DENIED_ENTITIES, Settings  # noqa: E402
from custodio.pii import EntityHit  # noqa: E402
from custodio.proxy import create_app  # noqa: E402


# ----------------------------- inject_guidance ----------------------------- #
def test_inject_into_string_system():
    payload = {"system": "You are helpful.", "messages": []}
    inject_guidance(payload)
    assert payload["system"].startswith("You are helpful.")
    assert payload["system"].endswith(PLACEHOLDER_GUIDANCE)


def test_inject_into_block_system():
    original = {"type": "text", "text": "You are helpful.",
                "cache_control": {"type": "ephemeral"}}
    payload = {"system": [dict(original)], "messages": []}
    inject_guidance(payload)
    blocks = payload["system"]
    assert blocks[0] == original  # client's cached prefix untouched
    assert blocks[1] == {"type": "text", "text": PLACEHOLDER_GUIDANCE}


def test_inject_creates_system_when_absent():
    payload = {"messages": []}
    inject_guidance(payload)
    assert payload["system"] == PLACEHOLDER_GUIDANCE


# ------------------------------ proxy wiring ------------------------------- #
class NoopEngine:
    def process_span(self, text, entity_mapping, reserved=None):
        return text, [], []


class JaneEngine:
    def process_span(self, text, entity_mapping, reserved=None):
        hits = []
        if "Jane Doe" in text:
            per = entity_mapping.setdefault("PERSON", {})
            per.setdefault("Jane Doe", "<PERSON_0>")
            text = text.replace("Jane Doe", "<PERSON_0>")
            hits.append(EntityHit("PERSON", "<PERSON_0>", "***", 0.9))
        return text, hits, []


def _run_proxy(settings, engine, body):
    seen = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "application/json"},
                              json={"type": "message", "content": []})

    app = create_app(settings)
    tc = TestClient(app)
    tc.__enter__()
    try:
        core = app.state.custodio
        core._engine = engine
        core.client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream),
            base_url=settings.upstream_base_url,
        )
        r = tc.post("/v1/messages", json=body)
        assert r.status_code == 200
        return seen["body"]
    finally:
        tc.__exit__(None, None, None)


def test_proxy_appends_guidance_by_default():
    sent = _run_proxy(
        Settings(upstream_base_url="https://upstream.test"),
        JaneEngine(),
        {"model": "claude-x", "system": "Help Jane Doe.",
         "messages": [{"role": "user", "content": "hi"}]},
    )
    assert sent["system"] == "Help <PERSON_0>.\n\n" + PLACEHOLDER_GUIDANCE
    # The notice itself is injected after anonymization — its example tokens
    # arrive verbatim, and no PII from it can have been scanned.
    assert "<PERSON_0>" in sent["system"]


def test_proxy_guidance_can_be_disabled():
    sent = _run_proxy(
        Settings(upstream_base_url="https://upstream.test", inject_guidance=False),
        NoopEngine(),
        {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert "system" not in sent


# ------------------------------ config defaults ---------------------------- #
def _from_env(**env):
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return Settings.from_env()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_denied_entities_default_includes_nrp():
    assert "NRP" in Settings().denied_entities
    assert Settings().denied_entities == DEFAULT_DENIED_ENTITIES
    assert _from_env(CUSTODIO_DENIED_ENTITIES=None).denied_entities == (
        DEFAULT_DENIED_ENTITIES
    )


def test_denied_entities_env_replaces_default():
    s = _from_env(CUSTODIO_DENIED_ENTITIES="DATE_TIME,URL")
    assert s.denied_entities == ["DATE_TIME", "URL"]  # NRP not implied


def test_denied_entities_empty_env_denies_nothing():
    assert _from_env(CUSTODIO_DENIED_ENTITIES="").denied_entities == []


def test_inject_guidance_env():
    assert _from_env(CUSTODIO_INJECT_GUIDANCE=None).inject_guidance is True
    assert _from_env(CUSTODIO_INJECT_GUIDANCE="false").inject_guidance is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
