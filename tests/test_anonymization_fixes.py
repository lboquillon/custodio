"""Regression tests for confirmed PII anonymization fixes (payload coverage,
placeholder-collision reservation, and regex-engine detector quality)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presidio_anonymizer import AnonymizerEngine, OperatorConfig  # noqa: E402
from presidio_anonymizer.entities import RecognizerResult  # noqa: E402

from custodio.anthropic_payload import anonymize_request  # noqa: E402
from custodio.config import Settings  # noqa: E402
from custodio.operators import InstanceCounterAnonymizer, find_placeholders  # noqa: E402
from custodio.pii import EntityHit  # noqa: E402
from custodio.regex_engine import RegexEngine  # noqa: E402


def fake_anon(text):
    mapping = {"Jane Doe": "<PERSON_0>", "jane@acme.com": "<EMAIL_ADDRESS_0>"}
    out, hits = text, []
    for original, ph in mapping.items():
        if original in out:
            out = out.replace(original, ph)
            etype = "PERSON" if "PERSON" in ph else "EMAIL_ADDRESS"
            hits.append(EntityHit(etype, ph, "***", 0.9))
    return out, hits


S = Settings()


# ---------------------- payload coverage (leak vectors) ------------------ #
def test_document_text_source_anonymized():
    payload = {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                        "data": "Contract for Jane Doe"}},
    ]}]}
    anonymize_request(payload, fake_anon, S)
    assert payload["messages"][0]["content"][0]["source"]["data"] == "Contract for <PERSON_0>"


def test_document_content_blocks_anonymized():
    payload = {"messages": [{"role": "user", "content": [
        {"type": "document", "source": {"type": "content", "content": [
            {"type": "text", "text": "email jane@acme.com"},
        ]}},
    ]}]}
    anonymize_request(payload, fake_anon, S)
    inner = payload["messages"][0]["content"][0]["source"]["content"][0]
    assert inner["text"] == "email <EMAIL_ADDRESS_0>"


def test_thinking_block_reanonymized_on_request():
    payload = {"messages": [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "The user Jane Doe wants help"},
    ]}]}
    anonymize_request(payload, fake_anon, S)
    assert payload["messages"][0]["content"][0]["thinking"] == "The user <PERSON_0> wants help"


def test_metadata_user_id_anonymized_by_default():
    payload = {"messages": [], "metadata": {"user_id": "jane@acme.com"}}
    anonymize_request(payload, fake_anon, S)
    assert payload["metadata"]["user_id"] == "<EMAIL_ADDRESS_0>"


def test_metadata_not_touched_when_disabled():
    s = Settings(anonymize_metadata=False)
    payload = {"messages": [], "metadata": {"user_id": "jane@acme.com"}}
    anonymize_request(payload, fake_anon, s)
    assert payload["metadata"]["user_id"] == "jane@acme.com"


def test_tool_input_schema_anonymized_when_defs_enabled():
    s = Settings(anonymize_tool_defs=True)
    payload = {"messages": [], "tools": [{"name": "notify", "description": "x",
        "input_schema": {"properties": {"cc": {"default": "jane@acme.com"}}}}]}
    anonymize_request(payload, fake_anon, s)
    assert payload["tools"][0]["input_schema"]["properties"]["cc"]["default"] == "<EMAIL_ADDRESS_0>"


# --------------------- placeholder-collision reservation ----------------- #
def test_reserved_placeholder_not_reused():
    anonymizer = AnonymizerEngine()
    anonymizer.add_anonymizer(InstanceCounterAnonymizer)
    text = "Discuss <PERSON_0> with Alice Smith"
    reserved = find_placeholders(text)
    assert reserved == {"<PERSON_0>"}
    a = text.index("Alice Smith")
    entity_mapping = {}
    out = anonymizer.anonymize(
        text=text,
        analyzer_results=[RecognizerResult("PERSON", a, a + len("Alice Smith"), 0.9)],
        operators={"DEFAULT": OperatorConfig(
            "entity_counter", {"entity_mapping": entity_mapping, "reserved": reserved})},
    )
    # Alice Smith must NOT become <PERSON_0> (the user's own literal token).
    assert entity_mapping["PERSON"]["Alice Smith"] == "<PERSON_1>"
    assert out.text == "Discuss <PERSON_0> with <PERSON_1>"


# --------------------------- regex detector quality ---------------------- #
def _hits(text, **overrides):
    eng = RegexEngine(Settings(engine="regex", **overrides))
    out, hits, misses = eng.process_span(text, {})
    return out, hits, misses


def test_credit_card_requires_luhn():
    # valid Luhn card -> anonymized; random 16-digit run -> not a card
    _, hits, _ = _hits("card 4111 1111 1111 1111")
    assert any(h.entity_type == "CREDIT_CARD" for h in hits)
    _, hits2, _ = _hits("tracking 1234567890123456")
    assert not any(h.entity_type == "CREDIT_CARD" for h in hits2)


def test_ip_rejects_out_of_range_and_versions():
    _, hits, _ = _hits("connect to 192.168.1.10")
    assert any(h.entity_type == "IP_ADDRESS" for h in hits)
    _, hits2, _ = _hits("upgrade to version 1.2.3.4 now and 999.999.999.999")
    # 1.2.3.4 is valid-range so it IS caught; 999.* must NOT be.
    types = [(h.entity_type) for h in hits2]
    assert "IP_ADDRESS" in types
    out2, _, _ = _hits("bad 999.999.999.999 addr")
    assert "999.999.999.999" in out2  # invalid octets left untouched


def test_phone_ignores_bare_short_digit_runs():
    _, hits, _ = _hits("your order 1234567 shipped")
    assert not any(h.entity_type == "PHONE_NUMBER" for h in hits)
    _, hits2, _ = _hits("call +1 415 555 2671")
    assert any(h.entity_type == "PHONE_NUMBER" for h in hits2)


def test_iban_handles_spacing_and_checksum():
    _, hits, _ = _hits("pay to GB29 NWBK 6016 1331 9268 19 today")
    assert any(h.entity_type == "IBAN_CODE" for h in hits)  # spaced IBAN caught
    out, hits2, _ = _hits("token AB12CDEFGHIJKLM here")
    assert not any(h.entity_type == "IBAN_CODE" for h in hits2)  # fails mod-97


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
