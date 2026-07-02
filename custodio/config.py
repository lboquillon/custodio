# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Runtime configuration for Custodio, sourced from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str] | None:
    raw = os.getenv(name)
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    """All knobs for the proxy. Instantiate via :meth:`from_env`."""

    # Where real Anthropic traffic goes.
    upstream_base_url: str = "https://api.anthropic.com"

    # Which detection engine: "presidio" (full NER, needs spaCy; recommended) or
    # "regex" (regex + checksums, no spaCy; lower recall, minimal footprint).
    engine: str = "presidio"

    # Presidio / analysis
    language: str = "en"
    # spaCy model. en_core_web_lg = best recall, en_core_web_sm = fast to install.
    spacy_model: str = "en_core_web_lg"
    # Main detection threshold: spans at/above this score get anonymized.
    score_threshold: float = 0.5
    # Shadow pass threshold: spans in [shadow, main) are reported as "possible
    # misses" but NOT anonymized. Set >= score_threshold to disable the pass.
    shadow_threshold: float = 0.3
    # If set, only these entity types are anonymized. None = all supported.
    allowed_entities: list[str] | None = None
    # Entity types to never anonymize (wins over allowed_entities).
    denied_entities: list[str] = field(default_factory=list)

    # What to walk in the payload
    anonymize_system: bool = True
    anonymize_tool_inputs: bool = True   # tool_use.input + tool_result content
    anonymize_tool_defs: bool = False    # tools[] schemas — off: breaks tools
    anonymize_metadata: bool = True      # metadata.user_id (often an email/username)

    # Reject requests whose body exceeds this many bytes (0 = no limit). Guards
    # against a single huge payload monopolizing CPU/memory.
    max_body_bytes: int = 25 * 1024 * 1024

    # Audit
    audit_capacity: int = 500            # ring buffer / Redis retention size
    store_full_pii: bool = False         # if True, audit keeps clear-text values
    audit_jsonl_path: str | None = None  # optional append-only log file
    # Redis-backed audit store. When set, events are persisted to Redis and
    # fanned out to live dashboards via Redis pub/sub (so multiple proxy
    # workers/instances share one audit view). Unset = in-memory store.
    redis_url: str | None = None
    redis_prefix: str = "custodio"       # key/channel namespace
    redis_ttl_seconds: int = 0           # per-event TTL; 0 = no expiry
    # If set, the /custodio/* audit surface (dashboard, events, stream) requires
    # this token via `Authorization: Bearer <token>` or `?token=<token>`.
    # /custodio/health and static assets stay public. Unset = open (local use).
    audit_token: str | None = None

    # Networking
    request_timeout_seconds: float = 600.0

    # Safety: if the PII engine can't load, do we block (fail closed, default)
    # or forward un-anonymized traffic (fail open)? For a privacy tool, closed.
    fail_open: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            upstream_base_url=os.getenv(
                "CUSTODIO_UPSTREAM", "https://api.anthropic.com"
            ).rstrip("/"),
            engine=os.getenv("CUSTODIO_ENGINE", "presidio"),
            language=os.getenv("CUSTODIO_LANGUAGE", "en"),
            spacy_model=os.getenv("CUSTODIO_SPACY_MODEL", "en_core_web_lg"),
            score_threshold=float(os.getenv("CUSTODIO_SCORE_THRESHOLD", "0.5")),
            shadow_threshold=float(os.getenv("CUSTODIO_SHADOW_THRESHOLD", "0.3")),
            allowed_entities=_env_list("CUSTODIO_ALLOWED_ENTITIES"),
            denied_entities=_env_list("CUSTODIO_DENIED_ENTITIES") or [],
            anonymize_system=_env_bool("CUSTODIO_ANONYMIZE_SYSTEM", True),
            anonymize_tool_inputs=_env_bool("CUSTODIO_ANONYMIZE_TOOL_INPUTS", True),
            anonymize_tool_defs=_env_bool("CUSTODIO_ANONYMIZE_TOOL_DEFS", False),
            anonymize_metadata=_env_bool("CUSTODIO_ANONYMIZE_METADATA", True),
            max_body_bytes=int(os.getenv("CUSTODIO_MAX_BODY_BYTES", str(25 * 1024 * 1024))),
            audit_capacity=int(os.getenv("CUSTODIO_AUDIT_CAPACITY", "500")),
            store_full_pii=_env_bool("CUSTODIO_STORE_FULL_PII", False),
            audit_jsonl_path=os.getenv("CUSTODIO_AUDIT_JSONL") or None,
            redis_url=os.getenv("CUSTODIO_REDIS_URL") or None,
            redis_prefix=os.getenv("CUSTODIO_REDIS_PREFIX", "custodio"),
            redis_ttl_seconds=int(os.getenv("CUSTODIO_REDIS_TTL_SECONDS", "0")),
            audit_token=os.getenv("CUSTODIO_AUDIT_TOKEN") or None,
            request_timeout_seconds=float(
                os.getenv("CUSTODIO_TIMEOUT_SECONDS", "600")
            ),
            fail_open=_env_bool("CUSTODIO_FAIL_OPEN", False),
        )
