# Changelog

All notable changes to Custodio are documented here. This project adheres to
[Semantic Versioning](https://semver.org/) and the format of
[Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] — 2026-07-02

First production release.

### Added
- **Redis-backed audit store** (`CUSTODIO_REDIS_URL`): events are persisted and
  fanned out to live dashboards over Redis pub/sub, so multiple workers or
  instances share one audit view and the trail survives restarts. In-memory
  remains the zero-dependency default; on Redis failure Custodio falls back to
  memory without affecting anonymization.
- **Live dashboard** over Server-Sent Events (`GET /custodio/stream`) — no
  polling; new requests appear instantly and the open detail refreshes in place.
  Deep-link a request with `?event=<id>`. Fonts are self-hosted (no font CDN).
- **Optional authentication** for the audit surface via `CUSTODIO_AUDIT_TOKEN`
  (protects `/custodio/*` except health and static assets).
- **Maximum-accuracy option**: transformer model support (`en_core_web_trf`) via
  the `transformers` extra and `make run-max`.
- **Request-size cap** (`CUSTODIO_MAX_BODY_BYTES`, default 25 MB → HTTP 413).
- Coverage for `document` blocks, re-sent `thinking` blocks, `metadata.user_id`,
  and tool `input_schema` (when tool-def anonymization is enabled).
- GitHub Actions: CI (lint + tests) and a release pipeline that publishes a
  Docker image to GHCR and the package to PyPI on a version tag.
- `CHANGELOG.md`, `SECURITY.md`.

### Changed
- Detection engine values: `demo` → `regex` (an honest name for the
  dependency-light, no-spaCy engine). `presidio` (full spaCy NER) remains the
  default and recommended engine; local `make run` now uses `en_core_web_lg`.
- Anonymization now runs off the event loop, so concurrent requests no longer
  serialize behind spaCy.
- The audit event is finalized exactly once, even on client disconnect; the
  JSONL log records one line per event.
- Docker publishes the port on `127.0.0.1` by default (localhost-only).

### Fixed
- **Fail-closed on un-parseable bodies.** A non-JSON or `Content-Encoding`
  (e.g. gzip) request body on `/v1/messages` is no longer forwarded raw; it is
  rejected (HTTP 415) unless `CUSTODIO_FAIL_OPEN=true`. `content-encoding` is
  stripped from forwarded requests.
- Streaming: de-anonymize `thinking_delta`; JSON-escape restored values inside
  streamed tool inputs (`input_json_delta`); decode multibyte UTF-8 split across
  network chunks; flush held-back text on a truncated stream.
- Placeholder-collision guard: a client's literal `<PERSON_0>` token can no
  longer be reused or reversed into unrelated data.
- Regex engine detectors: Luhn check for cards, IPv4 octet-range validation,
  IBAN spacing + mod-97, and a tighter phone matcher.

[1.0.0]: https://github.com/lboquillon/custodio/releases/tag/v1.0.0
