# Custodio — a transparent PII-anonymizing proxy for the Anthropic API

Custodio sits between any Anthropic client (Claude Code, the SDK, `curl`) and
`api.anthropic.com`. You point the client at Custodio with a single env var:

```bash
export ANTHROPIC_BASE_URL=http://localhost:3000
```

From then on every request is **anonymized before it leaves your machine** and
every response is **de-anonymized before the client sees it** — so the model
works with realistic-but-fake placeholders, while you keep reading and writing
real data. A built-in audit log shows exactly *what was anonymized and what
wasn't* on every request.

```
  Claude Code / SDK / curl
        │  ANTHROPIC_BASE_URL=http://localhost:3000
        │  POST /v1/messages   { system, messages, tools, stream }
        ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                      CUSTODIO PROXY                           │
  │                                                              │
  │  1. parse Anthropic payload, pull out the natural-language   │
  │     spans (system prompt, message content, tool_result,      │
  │     tool_use inputs)                                          │
  │  2. Presidio Analyzer → detect PII spans (PERSON, EMAIL,     │
  │     PHONE_NUMBER, CREDIT_CARD, IP, …)                        │
  │  3. InstanceCounter anonymizer → replace each PII value with │
  │     a stable placeholder <PERSON_0>, <EMAIL_ADDRESS_1>, …    │
  │     building a per-request `entity_mapping`                   │
  │  4. re-inject placeholders into the payload; append a system │
  │     notice telling the model to echo placeholders verbatim   │
  │  5. record an AUDIT event (entities, scores, preview,        │
  │     "possible misses" from a shadow low-threshold pass)      │
  │                                                              │
  │  ────────────  forward to api.anthropic.com  ────────────    │
  │                                                              │
  │  6a. non-stream: walk response JSON, replace placeholders    │
  │      back to originals using entity_mapping                  │
  │  6b. stream (SSE): de-anonymize text_delta / input_json_delta│
  │      on the fly with a placeholder-safe buffer               │
  └─────────────────────────────────────────────────────────────┘
        │
        ▼
  api.anthropic.com   (only ever sees <PERSON_0>, never "Jane Doe")
```

Example round-trip (invisible to the client):

| stage | text |
|-------|------|
| client sends | `My name is Jane Doe, email jane@acme.com` |
| → Anthropic sees | `My name is <PERSON_0>, email <EMAIL_ADDRESS_0>` |
| Anthropic replies | `Hi <PERSON_0>, I'll email <EMAIL_ADDRESS_0>.` |
| ← client receives | `Hi Jane Doe, I'll email jane@acme.com.` |

---

## How Presidio makes this reversible

Presidio has three engines (from the `presidio-analyzer` and
`presidio-anonymizer` packages):

- **`AnalyzerEngine.analyze(text, language)`** → a list of `RecognizerResult`
  (`entity_type`, `start`, `end`, `score`). This is the *detection* step,
  powered by a spaCy NER model plus regex/checksum recognizers.
- **`AnonymizerEngine.anonymize(text, analyzer_results, operators)`** → applies
  an *operator* to each detected span. Built-in operators (`replace`, `redact`,
  `mask`, `hash`, `encrypt`) are mostly **one-way**.
- **`DeanonymizeEngine.deanonymize(...)`** → the reverse direction, used by
  reversible operators.

The key to reversibility is a **custom operator pair** shipped in Presidio's own
OpenAI sample (`docs/samples/deployments/openai-anonymaztion-...`), which
Custodio adopts and hardens:

- **`InstanceCounterAnonymizer`** — replaces the *n*-th distinct `PERSON` with
  `<PERSON_0>`, `<PERSON_1>`, … The mapping (`entity_mapping`) is a plain dict
  `{entity_type: {original_value: placeholder}}`. Because the dict is passed in
  and mutated, the **same value always maps to the same placeholder within a
  request**, even across the system prompt and multiple messages.
- **`InstanceCounterDeanonymizer`** — inverts the mapping.

Custodio keeps `entity_mapping` **per request** (see "Session model" below), so
the whole thing is stateless and there's no PII sitting in a database.

---

## Why the proxy is *simpler* than a session API

Presidio's reference sample exposes explicit `/anonymize` + `/deanonymize`
endpoints and stores mappings in Redis keyed by a client-supplied `session_id`.
A transparent proxy doesn't need any of that:

- **Claude Code re-sends the full conversation on every turn.** So each
  `/v1/messages` request is self-contained. We build a fresh `entity_mapping`
  from the whole payload, use it to de-anonymize *that request's* response, then
  throw it away. No cross-request state, no Redis, no session IDs, no leakage
  between users.
- Placeholders never need to be stable across turns — the model only ever sees
  one request's placeholders and answers in kind.

This makes the mapping's lifetime exactly one request/response cycle, which is
also the safest possible design for a privacy tool.

---

## The four hard parts

### 1. Knowing which fields to touch
An Anthropic request is a nested structure. We must anonymize the
**conversation content** but *not* structural or schema fields, or we'd corrupt
the protocol / break Claude Code's tools. Custodio walks a targeted set of
fields (`anthropic_payload.py`):

- `system` (string, or list of `{type:"text", text}` blocks)
- `messages[].content` — string, or blocks: `text`, `tool_result.content`,
  `tool_use.input` (recursively, string leaves only), `document.source`
  (inline `text` data or nested `content` blocks), and re-sent `thinking`
  blocks (which carry real, already-restored PII from a prior turn)
- `metadata.user_id` — frequently a raw email/username, not an opaque id
- **tool *definitions* (`tools[]`) are skipped by default** — they're schemas,
  not user data, and anonymizing them breaks tool semantics. When
  `anonymize_tool_defs` is on, both the description and the `input_schema` string
  leaves (defaults/enums/examples) are walked.

A body that cannot be parsed as plain JSON (empty, malformed, or
`Content-Encoding`-compressed) is **not** forwarded on the anonymized endpoints:
it fails closed (HTTP 415) unless `fail_open` is set, because a raw passthrough
would leak un-anonymized content. A configurable `max_body_bytes` cap rejects
oversized bodies (HTTP 413) before they are buffered and analyzed.

### 2. Round-tripping through tool use
When Claude emits a `tool_use` like `{"file_path": "/Users/<PERSON_0>/x"}`,
Custodio de-anonymizes it **before** Claude Code executes the tool, so the tool
runs on the real path. The result comes back in the next request's
`tool_result`, where Custodio re-anonymizes it. The invariant that makes this
safe: *de-anonymize everything on the way in to the client, re-anonymize
everything on the way out.*

### 3. Making the model cooperate
Substitution alone is not enough: a model that sees `<PERSON_0>` with no
explanation treats it as redaction damage — it tells the user "a privacy filter
masked your details", refuses to use the tokens, and answers around them with
fill-in-the-blank templates. De-anonymization then never fires and the
transparency collapses. So after anonymizing, Custodio appends a short notice
to the system prompt (`PLACEHOLDER_GUIDANCE` in `anthropic_payload.py`,
`CUSTODIO_INJECT_GUIDANCE` to disable): placeholders are aliases for real
values, treat them as the real thing, echo them verbatim everywhere (prose,
code, tool calls), and never mention the substitution. Details that matter:

- **Injected after anonymization**, so the notice itself is never scanned and
  its example tokens can't perturb detection.
- **Appended, not prepended**, so a client's cache-controlled system prefix
  (Claude Code marks its system blocks with `cache_control`) stays
  byte-identical and prompt-cache hits survive.
- Also injected on `count_tokens`, so token counts match what `/v1/messages`
  actually sends.

Relatedly, the default entity set matters as much as the prompt: spaCy's `NRP`
type (nationalities, religions, political groups) matches everyday words like
"Japanese" that are usually the *subject* of a request rather than anyone's
identity — masking them makes tasks like "translate this to Japanese"
impossible. `NRP` is therefore in `DEFAULT_DENIED_ENTITIES` (opt back in with
an empty `CUSTODIO_DENIED_ENTITIES`).

### 4. Streaming (Claude Code always streams)
The response is an SSE stream of `content_block_delta` events. A placeholder
like `<PERSON_0>` can be **split across two `text_delta` chunks** (`<PER` …
`SON_0>`). `streaming.py` keeps a per-content-block buffer and only flushes text
that cannot be part of an unclosed placeholder — see that file for the exact
"safe split" rule. Both `text_delta` and `input_json_delta` (streamed tool
inputs) are de-anonymized this way.

---

## Observability — "what was anonymized and what wasn't"

Every request produces an **audit event** (`audit.py`), queryable at
`GET /custodio/events` and viewable at `GET /custodio/dashboard`:

- entities replaced: type, placeholder, masked original, confidence score
- the exact anonymized text that was sent upstream (so a human can eyeball leaks)
- **possible misses**: a second *shadow* analyzer pass at a lower score
  threshold; anything it catches that the main pass didn't is flagged as
  "possible PII below threshold — NOT anonymized" (i.e. that value *was* sent
  upstream). This is the closest we can get to surfacing false negatives.
- counts, model, streaming flag, latency

By default original values are **masked** in the audit store (`j••@acme.com`);
`STORE_FULL_PII=true` keeps them in the clear for debugging only.

**Live, not polled.** The dashboard subscribes to `GET /custodio/stream`
(Server-Sent Events). An in-process `EventBus` fans every add/update out to
connected dashboards, so new requests appear instantly and the open detail
refreshes in place. The event is published twice — once when it is registered
(so an in-flight or failing request is visible immediately) and once when it is
finalized with status/latency — and the dashboard upserts by id.

**Storage is pluggable.** The default `MemoryAuditStore` is a bounded ring
buffer (per process, zero dependencies). Setting `CUSTODIO_REDIS_URL` swaps in
`RedisAuditStore`, which persists each event, keeps a capacity-bounded index,
and publishes ids on a Redis pub/sub channel; a background task bridges that
channel back into the local `EventBus`, so several proxy workers/instances share
one live audit view. Redis is best-effort: if it is unreachable, Custodio logs a
warning and never lets an audit failure affect anonymization or the proxied
response.

---

## Known limitations (be honest about these)

- **Detection is not perfect.** Presidio is recall-limited; some PII will slip
  through. The shadow pass and the anonymized-payload preview help you *see*
  this, but nothing guarantees 100% coverage.
- **The model must echo placeholders verbatim** for de-anonymization to fire.
  The injected guidance (hard part 3) makes this reliable, but if the model
  still paraphrases `<PERSON_0>` into something else, that instance won't be
  restored (it'll just show the placeholder).
- **Opaque placeholders vs. fake surrogates.** Custodio uses opaque
  `<PERSON_0>` tokens because they round-trip most reliably. Faker-style fake
  names read more naturally but the model may inflect them, breaking the
  reverse lookup. Configurable later.
- **Auth pass-through.** Custodio forwards the client's `authorization`,
  `x-api-key`, `anthropic-beta`, and `anthropic-version` headers untouched, so
  both API-key auth and a Claude Pro/Max subscription login keep working with no
  key configured on Custodio itself.
