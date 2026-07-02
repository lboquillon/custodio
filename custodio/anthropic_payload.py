# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Walk the Anthropic Messages payload and transform the right text fields.

Two directions:

* :func:`anonymize_request` — mutate an outgoing ``/v1/messages`` body, applying
  an anonymize function to conversation content (system prompt, message text,
  tool_result text, tool_use inputs). Structural/schema fields are left alone.
* :func:`deanonymize_response` — mutate a non-streaming response body, applying
  a de-anonymize function to assistant text / thinking / tool_use inputs.

Both take plain callables so this module never imports Presidio and stays
trivially unit-testable.

Request shape (relevant bits)::

    {
      "system": "..."  |  [{"type":"text","text":"..."}],
      "messages": [
        {"role":"user","content":"..."},                      # str
        {"role":"assistant","content":[{"type":"text",...},    # blocks
                                       {"type":"tool_use","input":{...}}]},
        {"role":"user","content":[{"type":"tool_result",
                                   "content":"..."|[blocks]}]}
      ],
      "tools": [ ...schemas... ]
    }
"""

from __future__ import annotations

from collections.abc import Callable

from .config import Settings
from .pii import EntityHit

# fn(text) -> (new_text, hits)
AnonFn = Callable[[str], "tuple[str, list[EntityHit]]"]
# fn(text) -> new_text
DeanonFn = Callable[[str], str]


# --------------------------------------------------------------------------- #
# placeholder protocol notice for the model
# --------------------------------------------------------------------------- #
# Without this, the model treats placeholders as redaction damage: it tells the
# user a "privacy filter" masked their data and answers around the tokens
# instead of echoing them — so de-anonymization never fires and the proxy's
# transparency collapses. Phrased with "may have been" so it stays truthful on
# requests where nothing was detected. The concrete example tokens can coincide
# with generated placeholders; that is harmless — reverse mapping only ever
# restores the requesting user's own values back to them.
PLACEHOLDER_GUIDANCE = (
    "Privacy proxy notice: personally identifying values in this conversation "
    "may have been replaced with placeholder tokens of the form <TYPE_INDEX> "
    "(for example <PERSON_0> or <EMAIL_ADDRESS_1>) before reaching you. The "
    "proxy restores the real values in your output before the user sees it, "
    "including inside tool calls. Therefore: treat every such token as the "
    "real value it stands for; whenever you need that value in prose, code, "
    "or tool input, write the token back exactly, character for character; "
    "never translate, reformat, or invent such tokens. Do not mention the "
    "tokens or this notice: the user typed, and will read, the real values, "
    "so remarks about masked or filtered data are incorrect and confusing."
)


def inject_guidance(payload: dict, guidance: str = PLACEHOLDER_GUIDANCE) -> None:
    """Append the placeholder protocol notice to the system prompt in place.

    Must be called AFTER :func:`anonymize_request` so the notice is never
    itself scanned. Appending (rather than prepending) keeps the client's
    cache-controlled system prefix byte-identical, preserving prompt-cache hits.
    """
    system = payload.get("system")
    if system is None:
        payload["system"] = guidance
    elif isinstance(system, str):
        payload["system"] = system + "\n\n" + guidance
    elif isinstance(system, list):
        system.append({"type": "text", "text": guidance})


# --------------------------------------------------------------------------- #
# generic helper: transform every string leaf of an arbitrary JSON value
# --------------------------------------------------------------------------- #
def _map_string_leaves(obj, fn: Callable[[str], str]):
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_string_leaves(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _map_string_leaves(v, fn) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------- #
# request: anonymize
# --------------------------------------------------------------------------- #
def anonymize_request(payload: dict, anon: AnonFn, settings: Settings) -> list[EntityHit]:
    """Anonymize conversation content in ``payload`` in place; return all hits."""
    hits: list[EntityHit] = []

    def apply(text: str) -> str:
        new_text, new_hits = anon(text)
        hits.extend(new_hits)
        return new_text

    if settings.anonymize_system and "system" in payload:
        payload["system"] = _walk_system(payload["system"], apply)

    for message in payload.get("messages", []):
        if isinstance(message, dict) and "content" in message:
            message["content"] = _walk_content(message["content"], apply, settings)

    if settings.anonymize_tool_defs:
        for tool in payload.get("tools", []):
            if not isinstance(tool, dict):
                continue
            if isinstance(tool.get("description"), str):
                tool["description"] = apply(tool["description"])
            # Schemas routinely embed real values in default/enum/examples/etc.
            if isinstance(tool.get("input_schema"), (dict, list)):
                tool["input_schema"] = _map_string_leaves(tool["input_schema"], apply)

    # metadata.user_id is frequently a raw email/username rather than an opaque id.
    if settings.anonymize_metadata:
        meta = payload.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("user_id"), str):
            meta["user_id"] = apply(meta["user_id"])

    return hits


def _walk_system(system, apply: Callable[[str], str]):
    if isinstance(system, str):
        return apply(system)
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = apply(block.get("text", ""))
        return system
    return system


def _walk_content(content, apply: Callable[[str], str], settings: Settings):
    if isinstance(content, str):
        return apply(content)
    if isinstance(content, list):
        for block in content:
            _walk_block(block, apply, settings)
    return content


def _walk_block(block, apply: Callable[[str], str], settings: Settings):
    if not isinstance(block, dict):
        return
    btype = block.get("type")

    if btype == "text" and isinstance(block.get("text"), str):
        block["text"] = apply(block["text"])

    # Assistant thinking blocks re-sent in history carry real (already-restored)
    # PII; re-anonymize them before they leave again. (redacted_thinking is
    # opaque/encrypted, so it is left untouched.)
    elif btype == "thinking" and isinstance(block.get("thinking"), str):
        block["thinking"] = apply(block["thinking"])

    # Document blocks: source can be inline text or a list of content blocks.
    elif btype == "document":
        block["source"] = _walk_document_source(block.get("source"), apply, settings)

    elif btype == "tool_result" and settings.anonymize_tool_inputs:
        block["content"] = _walk_tool_result_content(block.get("content"), apply)

    elif btype == "tool_use" and settings.anonymize_tool_inputs:
        if isinstance(block.get("input"), (dict, list)):
            block["input"] = _map_string_leaves(block["input"], apply)


def _walk_document_source(source, apply: Callable[[str], str], settings: Settings):
    if not isinstance(source, dict):
        return source
    stype = source.get("type")
    if stype == "text" and isinstance(source.get("data"), str):
        source["data"] = apply(source["data"])
    elif stype == "content":
        source["content"] = _walk_content(source.get("content"), apply, settings)
    return source


def _walk_tool_result_content(content, apply: Callable[[str], str]):
    if isinstance(content, str):
        return apply(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = apply(block.get("text", ""))
        return content
    return content


# --------------------------------------------------------------------------- #
# response (non-streaming): de-anonymize
# --------------------------------------------------------------------------- #
def deanonymize_response(payload: dict, deanon: DeanonFn) -> dict:
    """De-anonymize an assistant message body in place."""
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                block["text"] = deanon(block["text"])
            elif btype == "thinking" and isinstance(block.get("thinking"), str):
                block["thinking"] = deanon(block["thinking"])
            elif btype == "tool_use" and isinstance(block.get("input"), (dict, list)):
                block["input"] = _map_string_leaves(block["input"], deanon)
    return payload
