# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Custodio — transparent PII-anonymizing reverse proxy for the Anthropic API.

Point any Anthropic client at this server:

    export ANTHROPIC_BASE_URL=http://localhost:3000

`POST /v1/messages` and `.../count_tokens` get their conversation content
anonymized before being forwarded to api.anthropic.com; message responses are
de-anonymized on the way back (streaming or not). Everything else is proxied
untouched. Audit data is at `/custodio/*`, with a live dashboard at
`/custodio/dashboard`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .anthropic_payload import anonymize_request, deanonymize_response
from .audit import (
    AuditEvent,
    EventBus,
    create_audit_store,
    entities_to_dicts,
    misses_to_dicts,
)
from .config import Settings
from .dashboard import DASHBOARD_HTML
from .operators import find_placeholders
from .pii import build_reverse_map, deanonymize_text
from .streaming import deanonymize_sse_stream

logger = logging.getLogger("custodio")

# Self-hosted dashboard fonts (no external/Google Fonts callout — this is a
# privacy tool). Only these exact files are served.
_ASSETS_DIR = Path(__file__).parent / "assets"
_ALLOWED_ASSETS = {
    "space-mono-400.woff2", "space-mono-700.woff2",
    "space-grotesk-500.woff2", "space-grotesk-700.woff2",
}

# Headers we must not forward upstream (hop-by-hop + ones httpx recomputes).
# accept-encoding is dropped so upstream replies uncompressed and we can rewrite
# the body freely. content-encoding is dropped because the anonymized body we
# forward is always plain (uncompressed) JSON, so a client's stale encoding
# header would misdescribe it.
_REQUEST_STRIP = {
    "host", "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "proxy-connection", "te", "trailer", "accept-encoding",
    "content-encoding",
}
# Headers we must not echo back to the client (body was rewritten / decoded).
_RESPONSE_STRIP = {
    "content-length", "content-encoding", "transfer-encoding", "connection",
    "keep-alive",
}


def _request_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _REQUEST_STRIP}


def _response_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _RESPONSE_STRIP}


class Custodio:
    """Holds the shared engine + http client + audit store + live event bus."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bus = EventBus()
        self.audit = None  # created in lifespan (Redis needs a running loop)
        self.client: httpx.AsyncClient | None = None
        self._engine = None
        self._engine_error: str | None = None

    def engine(self):
        """Lazily construct the detection engine (heavy). Cache the outcome."""
        if self._engine is None and self._engine_error is None:
            try:
                if self.settings.engine == "regex":
                    from .regex_engine import RegexEngine

                    logger.info("Loading regex engine (no spaCy; lower recall)…")
                    self._engine = RegexEngine(self.settings)
                else:
                    from .pii import PIIEngine

                    logger.info(
                        "Loading Presidio engine (spaCy=%s)…", self.settings.spacy_model
                    )
                    self._engine = PIIEngine(self.settings)
                logger.info("Engine ready: %s", self.settings.engine)
            except Exception as exc:  # noqa: BLE001
                self._engine_error = f"{type(exc).__name__}: {exc}"
                logger.error("Failed to load detection engine: %s", self._engine_error)
        if self._engine is None:
            raise RuntimeError(self._engine_error or "engine unavailable")
        return self._engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    core = Custodio(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        core.client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
        )
        core.audit = create_audit_store(settings, core.bus)
        await core.audit.start()
        try:
            yield
        finally:
            await core.audit.aclose()
            await core.client.aclose()

    app = FastAPI(title="Custodio", lifespan=lifespan)
    app.state.custodio = core

    def _authorized(request: Request) -> bool:
        """Check the audit token (constant-time). No token configured = open."""
        token = settings.audit_token
        if not token:
            return True
        supplied = None
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[len("Bearer "):]
        if supplied is None:
            # Cookie is preferred by the dashboard (keeps the token out of the
            # URL / access logs); query param is the fallback for bootstrapping.
            supplied = request.cookies.get("custodio_token")
        if supplied is None:
            supplied = request.query_params.get("token")
        return bool(supplied) and hmac.compare_digest(supplied, token)

    def _unauthorized() -> JSONResponse:
        return JSONResponse(
            {"type": "error", "error": {"type": "unauthorized",
                                        "message": "Missing or invalid audit token."}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ----------------------------- audit API ---------------------------- #
    @app.get("/custodio/health")
    async def health(request: Request):
        # Liveness probe stays public and minimal; operational details (upstream,
        # engine, backend) require the audit token when one is configured.
        if not _authorized(request):
            return {"status": "ok"}
        return {
            "status": "ok",
            "upstream": settings.upstream_base_url,
            "engine": settings.engine,
            "engine_loaded": core._engine is not None,
            "engine_error": core._engine_error,
            "audit_backend": getattr(core.audit, "backend", "memory"),
            "audit_protected": bool(settings.audit_token),
            "dashboard_clients": core.bus.subscriber_count,
        }

    @app.get("/custodio/stats")
    async def stats(request: Request):
        if not _authorized(request):
            return _unauthorized()
        return await core.audit.stats()

    @app.get("/custodio/events")
    async def events(request: Request, limit: int = 100):
        if not _authorized(request):
            return _unauthorized()
        return await core.audit.list(limit=limit)

    @app.get("/custodio/events/{event_id}")
    async def event_detail(event_id: str, request: Request):
        if not _authorized(request):
            return _unauthorized()
        ev = await core.audit.get(event_id)
        if ev is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return ev

    @app.get("/custodio/stream")
    async def stream(request: Request):
        """Server-Sent Events feed of audit updates — powers the live dashboard."""
        if not _authorized(request):
            return _unauthorized()
        queue = core.bus.subscribe()

        async def gen():
            try:
                yield b"retry: 3000\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"  # heartbeat keeps the connection warm
                        if await request.is_disconnected():
                            break
                        continue
                    data = json.dumps(msg, ensure_ascii=False)
                    yield f"event: {msg.get('type', 'message')}\ndata: {data}\n\n".encode()
            finally:
                core.bus.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/custodio/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not _authorized(request):
            return _unauthorized()
        return DASHBOARD_HTML

    @app.get("/custodio/assets/{name}")
    async def asset(name: str):
        if name not in _ALLOWED_ASSETS:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = _ASSETS_DIR / name
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return Response(
            path.read_bytes(),
            media_type="font/woff2",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # --------------------------- the proxy ------------------------------ #
    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    )
    async def proxy(full_path: str, request: Request):
        path_norm = full_path.rstrip("/")
        is_messages = request.method == "POST" and path_norm == "v1/messages"
        is_count = request.method == "POST" and path_norm == "v1/messages/count_tokens"
        if is_messages or is_count:
            return await _handle_anonymized(
                core, request, full_path, deanonymize=is_messages
            )
        return await _passthrough(core, request, full_path)

    return app


async def _passthrough(core: Custodio, request: Request, full_path: str) -> Response:
    body = await request.body()
    url = "/" + full_path
    upstream = await core.client.request(
        request.method,
        url,
        params=request.query_params,
        headers=_request_headers(request.headers),
        content=body,
    )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


def _too_large(limit: int) -> JSONResponse:
    return JSONResponse(
        {
            "type": "error",
            "error": {
                "type": "custodio_request_too_large",
                "message": f"Request body exceeds the {limit}-byte limit.",
            },
        },
        status_code=413,
    )


def _looks_like_json(request: Request, raw: bytes) -> bool:
    """Guard against silently proxying a body we can't inspect.

    A compressed or otherwise non-plain body would slip past ``json.loads`` and
    be forwarded raw (un-anonymized). We only treat a body as anonymizable JSON
    when it is not content-encoded.
    """
    if request.headers.get("content-encoding"):
        return False
    return bool(raw)


async def _handle_anonymized(
    core: Custodio, request: Request, full_path: str, deanonymize: bool
) -> Response:
    settings = core.settings
    started = time.monotonic()

    # --- reject oversized bodies before buffering/parsing ---------------- #
    max_bytes = settings.max_body_bytes
    if max_bytes and max_bytes > 0:
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > max_bytes:
            return _too_large(max_bytes)

    raw = await request.body()
    if max_bytes and max_bytes > 0 and len(raw) > max_bytes:
        return _too_large(max_bytes)

    # --- load engine (fail closed by default) --------------------------- #
    try:
        engine = core.engine()
    except RuntimeError as exc:
        if not settings.fail_open:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "custodio_unavailable",
                        "message": (
                            "PII engine failed to load; refusing to forward "
                            f"un-anonymized traffic. ({exc})"
                        ),
                    },
                },
                status_code=503,
            )
        logger.warning("fail_open: forwarding WITHOUT anonymization (%s)", exc)
        return await _passthrough(core, request, full_path)

    # A body we can't parse as plain JSON must NOT be forwarded raw when we are
    # meant to anonymize it — that would leak. Fail closed unless fail_open.
    if not _looks_like_json(request, raw):
        if settings.fail_open:
            logger.warning("fail_open: forwarding non-JSON/encoded body un-anonymized")
            return await _passthrough(core, request, full_path)
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "custodio_unprocessable",
                    "message": (
                        "Request body is not plain JSON (compressed or empty); "
                        "refusing to forward un-anonymized traffic."
                    ),
                },
            },
            status_code=415,
        )

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        if settings.fail_open:
            return await _passthrough(core, request, full_path)
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "custodio_unprocessable",
                    "message": "Request body is not valid JSON; refusing to forward.",
                },
            },
            status_code=415,
        )

    # --- anonymize the request (off the event loop; Presidio is CPU-bound) #
    entity_mapping: dict = {}
    misses: list = []
    # Reserve any placeholder-shaped token the client typed verbatim, so a
    # generated placeholder can never collide with (and be reversed into) it.
    reserved = find_placeholders(raw.decode("utf-8", "replace"))

    def anon(text: str):
        new_text, hits, span_misses = engine.process_span(
            text, entity_mapping, reserved=reserved
        )
        misses.extend(span_misses)
        return new_text, hits

    hits = await asyncio.to_thread(anonymize_request, payload, anon, settings)
    new_body = json.dumps(payload).encode("utf-8")
    reverse_map = build_reverse_map(entity_mapping)

    event = AuditEvent(
        id=uuid.uuid4().hex[:12],
        ts=time.time(),
        model=str(payload.get("model", "")),
        endpoint="/" + full_path,
        stream=bool(payload.get("stream", False)),
        entities=entities_to_dicts(hits),
        entity_count=len(hits),
        possible_misses=misses_to_dicts(misses),
        chars_in=len(raw),
        chars_out=len(new_body),
        anonymized_preview=new_body.decode("utf-8", "replace")[:4000],
    )
    # Register up front, so the request is visible even if upstream stalls/fails.
    await core.audit.add(event)

    async def finalize(status: int | None):
        event.status = status
        event.latency_ms = round((time.monotonic() - started) * 1000, 1)
        await core.audit.update(event)

    # --- forward upstream ----------------------------------------------- #
    url = "/" + full_path
    upstream_req = core.client.build_request(
        request.method,
        url,
        params=request.query_params,
        headers=_request_headers(request.headers),
        content=new_body,
    )
    try:
        upstream = await core.client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await finalize(502)
        return JSONResponse(
            {"type": "error", "error": {"type": "upstream_unreachable",
                                        "message": str(exc)}},
            status_code=502,
        )

    content_type = upstream.headers.get("content-type", "")
    out_headers = _response_headers(upstream.headers)

    # count_tokens (deanonymize=False) or non-message: return body as-is.
    if not deanonymize:
        body = await upstream.aread()
        await upstream.aclose()
        await finalize(upstream.status_code)
        return Response(body, status_code=upstream.status_code,
                        headers=out_headers, media_type=content_type)

    # streaming SSE response → de-anonymize on the fly.
    if "text/event-stream" in content_type:
        async def _finish(applied):
            event.response_placeholders = sorted(applied)
            await finalize(upstream.status_code)

        async def body_iter():
            try:
                async for chunk in deanonymize_sse_stream(
                    upstream.aiter_bytes(), reverse_map, on_finish=_finish
                ):
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body_iter(),
            status_code=upstream.status_code,
            headers=out_headers,
            media_type=content_type or "text/event-stream",
        )

    # non-streaming response.
    body = await upstream.aread()
    await upstream.aclose()

    if "application/json" in content_type and reverse_map:
        try:
            resp_json = json.loads(body)
            deanonymize_response(resp_json, lambda t: deanonymize_text(t, reverse_map))
            body = json.dumps(resp_json).encode("utf-8")
            event.response_placeholders = sorted(
                p for p in reverse_map if p in body.decode("utf-8", "replace")
            )
        except (ValueError, TypeError):
            pass

    await finalize(upstream.status_code)
    return Response(body, status_code=upstream.status_code,
                    headers=out_headers, media_type=content_type)


# module-level app for `uvicorn custodio.proxy:app`
app = create_app()
