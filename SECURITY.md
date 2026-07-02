# Security Policy

## Reporting a vulnerability

Please report security issues privately. Do **not** open a public issue for a
vulnerability.

- Use GitHub's [private vulnerability reporting](https://github.com/lboquillon/custodio/security/advisories/new), or
- email the maintainer: lboquillon@gmail.com

You will get an acknowledgement within a few days. Please include steps to
reproduce and the impact you observed.

## Supported versions

The latest 1.x release receives security fixes.

| Version | Supported |
|---------|-----------|
| 1.x     | Yes       |
| < 1.0   | No        |

## Operating Custodio safely

Custodio is a privacy tool; deploy it accordingly.

- **Anonymization is best-effort, not a guarantee.** Detection is recall-limited;
  some PII can slip through. Custodio surfaces this (the "possible misses" pass
  and the exact anonymized-payload preview) but does not promise 100% coverage.
  Review the dashboard for sensitive workloads.
- **Fail closed.** Keep `CUSTODIO_FAIL_OPEN=false` (the default). If the
  detection engine cannot load, or a request body cannot be parsed as plain
  JSON, Custodio refuses to forward it rather than leaking un-anonymized data.
- **Protect the audit surface.** The `/custodio/*` endpoints (dashboard, events)
  expose the exact bytes sent upstream and — if `CUSTODIO_STORE_FULL_PII=true` —
  clear-text originals. Set `CUSTODIO_AUDIT_TOKEN` and/or bind to localhost. The
  provided `docker compose` publishes the port on `127.0.0.1` only.
- **Keep `CUSTODIO_STORE_FULL_PII=false`** (the default) outside local debugging.
- **Credentials pass through untouched.** Custodio forwards your `authorization` /
  `x-api-key` / `anthropic-beta` headers to Anthropic and never logs them.
