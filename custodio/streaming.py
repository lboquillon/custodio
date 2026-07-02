# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Placeholder-safe de-anonymization of an Anthropic SSE response stream.

The response comes back as Server-Sent Events. Assistant text arrives in
``content_block_delta`` events whose ``delta.text`` (or ``delta.partial_json``
for streamed tool inputs) is an arbitrary substring of the full output. A
placeholder such as ``<PERSON_0>`` can therefore be split across two deltas::

    delta 1: "... hi <PER"
    delta 2: "SON_0>, nice to meet you"

If we replaced naively per-delta we'd miss it. So we buffer: we only release
text that provably cannot be the start of an unclosed placeholder, and hold the
rest until more arrives (or the block ends).

Nothing here imports Presidio; it works purely off the ``{placeholder: original}``
reverse map, so it is fully unit-testable.
"""

from __future__ import annotations

import codecs
import inspect
import json
import re
from collections.abc import AsyncIterator


class PlaceholderStreamDeanonymizer:
    """Streaming string replacer that never splits a placeholder mid-token."""

    def __init__(
        self,
        reverse_map: dict[str, str],
        applied: set[str] | None = None,
        json_mode: bool = False,
    ):
        self.reverse_map = reverse_map
        self.placeholders = sorted(reverse_map, key=len, reverse=True)
        self._pattern = (
            re.compile("|".join(re.escape(p) for p in self.placeholders))
            if self.placeholders
            else None
        )
        self._buffer = ""
        self.applied = applied if applied is not None else set()
        # When True the emitted text is spliced back into a JSON string literal
        # (streamed tool input), so originals must be JSON-escaped.
        self.json_mode = json_mode

    def feed(self, text: str) -> str:
        """Absorb a chunk; return the text that is safe to emit now."""
        if self._pattern is None:
            return text  # no placeholders in this request → identity fast-path
        self._buffer += text
        emit, self._buffer = self._safe_split(self._buffer)
        return self._replace(emit)

    def flush(self) -> str:
        """Emit whatever is still buffered (call at end of the content block)."""
        out = self._replace(self._buffer)
        self._buffer = ""
        return out

    # -- internals ------------------------------------------------------- #
    def _replace(self, text: str) -> str:
        if not self._pattern or not text:
            return text

        def sub(match: re.Match) -> str:
            token = match.group(0)
            self.applied.add(token)
            original = self.reverse_map[token]
            if self.json_mode:
                # Escape for insertion inside a JSON string (drop the quotes
                # json.dumps adds) so a value with " \ or control chars stays
                # valid JSON.
                return json.dumps(original)[1:-1]
            return original

        return self._pattern.sub(sub, text)

    def _safe_split(self, buf: str) -> tuple[str, str]:
        """Split ``buf`` into (emit_now, hold_back)."""
        idx = buf.rfind("<")
        if idx == -1:
            return buf, ""
        tail = buf[idx:]
        if ">" in tail:
            # The last '<' already has a closing '>', so no token is open.
            return buf, ""
        # Open '<...' with no '>': hold it only if it could still grow into a
        # real placeholder. Otherwise it's ordinary text (e.g. "a < b").
        if any(p.startswith(tail) for p in self.placeholders):
            return buf[:idx], tail
        return buf, ""


class SSEDeanonymizer:
    """Applies de-anonymization across a whole SSE event stream."""

    def __init__(self, reverse_map: dict[str, str]):
        self.reverse_map = reverse_map
        self.applied: set[str] = set()
        self._buffers: dict[int, PlaceholderStreamDeanonymizer] = {}
        self._last_delta_type: dict[int, str] = {}

    # delta.type -> the field carrying its text
    _DELTA_KEYS = {
        "text_delta": "text",
        "thinking_delta": "thinking",
        "input_json_delta": "partial_json",
    }

    def _buf(self, index: int, json_mode: bool = False) -> PlaceholderStreamDeanonymizer:
        if index not in self._buffers:
            self._buffers[index] = PlaceholderStreamDeanonymizer(
                self.reverse_map, applied=self.applied, json_mode=json_mode
            )
        return self._buffers[index]

    def process(self, event_name: str | None, data: dict) -> list[tuple[str, dict]]:
        """Transform one parsed event into the events to emit downstream."""
        etype = data.get("type")
        name = event_name or etype

        if etype == "content_block_delta":
            index = data.get("index", 0)
            delta = data.get("delta", {})
            dtype = delta.get("type")
            key = self._DELTA_KEYS.get(dtype)
            if key is not None:
                self._last_delta_type[index] = dtype
                buf = self._buf(index, json_mode=(dtype == "input_json_delta"))
                delta[key] = buf.feed(delta.get(key, ""))
            return [(name, data)]

        if etype == "content_block_stop":
            index = data.get("index", 0)
            out: list[tuple[str, dict]] = []
            emitted = self._emit_remainder(index)
            if emitted is not None:
                out.append(emitted)
            out.append((name, data))
            return out

        return [(name, data)]

    def _emit_remainder(self, index: int) -> tuple[str, dict] | None:
        """Flush a block's held-back tail as a content_block_delta, if any."""
        if index not in self._buffers:
            return None
        remainder = self._buffers[index].flush()
        if not remainder:
            return None
        dtype = self._last_delta_type.get(index, "text_delta")
        key = self._DELTA_KEYS.get(dtype, "text")
        return (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": dtype, key: remainder},
            },
        )

    def flush_all(self) -> list[tuple[str, dict]]:
        """Emit any residual buffers (e.g. a stream truncated before stop)."""
        out: list[tuple[str, dict]] = []
        for index in list(self._buffers):
            emitted = self._emit_remainder(index)
            if emitted is not None:
                out.append(emitted)
        return out


def _serialize(event_name: str | None, data_str: str) -> bytes:
    prefix = f"event: {event_name}\n" if event_name else ""
    return f"{prefix}data: {data_str}\n\n".encode()


def _dispatch(
    engine: SSEDeanonymizer, event_name: str | None, data_lines: list[str]
) -> list[bytes]:
    raw = "\n".join(data_lines)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return [_serialize(event_name, raw)]  # pass through anything non-JSON
    return [
        _serialize(ev, json.dumps(d, ensure_ascii=False))
        for ev, d in engine.process(event_name, data)
    ]


async def deanonymize_sse_stream(
    byte_iter: AsyncIterator[bytes],
    reverse_map: dict[str, str],
    on_finish=None,
) -> AsyncIterator[bytes]:
    """Wrap an upstream byte stream, yielding de-anonymized SSE bytes.

    ``on_finish`` (optional) is called once with the set of placeholders that
    were actually restored — handy for the audit log. It may be a plain callable
    or a coroutine function; it always runs, even if the client disconnects
    mid-stream (so the audit event is finalized exactly once).
    """
    engine = SSEDeanonymizer(reverse_map)
    # Incremental decoder so a multibyte UTF-8 codepoint split across two network
    # chunks is not corrupted (per-chunk decode would emit replacement chars).
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    text_buf = ""
    event_name: str | None = None
    data_lines: list[str] = []

    try:
        async for chunk in byte_iter:
            text_buf += decoder.decode(chunk)
            while "\n" in text_buf:
                line, text_buf = text_buf.split("\n", 1)
                line = line.rstrip("\r")
                if line == "":
                    if data_lines:
                        for out in _dispatch(engine, event_name, data_lines):
                            yield out
                    event_name, data_lines = None, []
                elif line.startswith("event:"):
                    event_name = line[len("event:") :].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].lstrip(" "))
                # ':' comments and other fields are ignored

        text_buf += decoder.decode(b"", final=True)
        if data_lines:  # flush a trailing event without terminating blank line
            for out in _dispatch(engine, event_name, data_lines):
                yield out
        # Emit any text held back for a block that never got content_block_stop
        # (e.g. an upstream-truncated stream), so nothing is silently dropped.
        for ev, d in engine.flush_all():
            yield _serialize(ev, json.dumps(d, ensure_ascii=False))
    finally:
        if on_finish is not None:
            result = on_finish(engine.applied)
            if inspect.isawaitable(result):
                await result


def deanonymize_sse_bytes(data: bytes, reverse_map: dict[str, str]) -> bytes:
    """Synchronous helper for tests: transform a complete SSE byte blob."""

    async def _run() -> bytes:
        async def _one() -> AsyncIterator[bytes]:
            yield data

        chunks: list[bytes] = []
        async for out in deanonymize_sse_stream(_one(), reverse_map):
            chunks.append(out)
        return b"".join(chunks)

    import asyncio

    return asyncio.run(_run())
