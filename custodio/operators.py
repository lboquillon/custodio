# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Reversible anonymize/de-anonymize operators for Presidio.

These are hardened versions of the ``InstanceCounter`` operators from
Presidio's own OpenAI sample
(``docs/samples/deployments/openai-anonymaztion-...``).

The idea: replace the n-th distinct value of an entity type with a stable
placeholder ``<PERSON_0>``, ``<PERSON_1>``, … and remember the mapping in a
plain dict so the substitution can be reversed later.

    entity_mapping = {
        "PERSON":        {"Jane Doe": "<PERSON_0>", "Bob": "<PERSON_1>"},
        "EMAIL_ADDRESS": {"jane@acme.com": "<EMAIL_ADDRESS_0>"},
    }

The dict is passed into every ``anonymize`` call and mutated in place, which is
what keeps placeholders consistent across every text span in a single request.

Presidio (the anonymizer package) is only imported here, lazily, so the rest of
Custodio can be imported and unit-tested without it installed.
"""

from __future__ import annotations

import re

from presidio_anonymizer.operators import Operator, OperatorType

# Placeholder shape, e.g. <PERSON_0> or <EMAIL_ADDRESS_12>. Entity types may
# themselves contain underscores, so the counter is the trailing _<digits>.
PLACEHOLDER_RE = re.compile(r"<([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)_(\d+)>")


def find_placeholders(text: str) -> set[str]:
    """Return every placeholder-shaped token already present in ``text``.

    Used to reserve tokens the user typed verbatim so a generated placeholder
    can never collide with (and later be reversed into) unrelated real PII.
    """
    if not text:
        return set()
    return {m.group(0) for m in PLACEHOLDER_RE.finditer(text)}


class InstanceCounterAnonymizer(Operator):
    """Replace each distinct entity value with ``<ENTITY_TYPE_index>``."""

    REPLACING_FORMAT = "<{entity_type}_{index}>"

    def operate(self, text: str, params: dict = None) -> str:
        entity_type: str = params["entity_type"]
        entity_mapping: dict[str, dict[str, str]] = params["entity_mapping"]
        reserved: set[str] = params.get("reserved") or set()

        per_type = entity_mapping.setdefault(entity_type, {})

        # Same value seen before in this request -> reuse its placeholder.
        if text in per_type:
            return per_type[text]

        index = self._next_index(per_type)
        new_placeholder = self.REPLACING_FORMAT.format(
            entity_type=entity_type, index=index
        )
        # Skip any token the user already typed verbatim, so reversal never maps
        # the user's own literal placeholder onto someone else's value.
        while new_placeholder in reserved:
            index += 1
            new_placeholder = self.REPLACING_FORMAT.format(
                entity_type=entity_type, index=index
            )
        per_type[text] = new_placeholder
        return new_placeholder

    @staticmethod
    def _next_index(per_type: dict[str, str]) -> int:
        """Next free counter for this entity type (0 if none yet)."""
        if not per_type:
            return 0
        indices = []
        for placeholder in per_type.values():
            match = PLACEHOLDER_RE.fullmatch(placeholder)
            if match:
                indices.append(int(match.group(2)))
        return (max(indices) + 1) if indices else 0

    def validate(self, params: dict = None) -> None:
        if "entity_mapping" not in params:
            raise ValueError("An input Dict called `entity_mapping` is required.")
        if "entity_type" not in params:
            raise ValueError("An entity_type param is required.")

    def operator_name(self) -> str:
        return "entity_counter"

    def operator_type(self) -> OperatorType:
        return OperatorType.Anonymize


class InstanceCounterDeanonymizer(Operator):
    """Replace a placeholder with the original text using ``entity_mapping``."""

    def operate(self, text: str, params: dict = None) -> str:
        entity_type: str = params["entity_type"]
        entity_mapping: dict[str, dict[str, str]] = params["entity_mapping"]

        per_type = entity_mapping.get(entity_type, {})
        for original, placeholder in per_type.items():
            if placeholder == text:
                return original
        # Unknown placeholder: leave it untouched rather than raising, so a
        # proxy never corrupts a response it can't fully reverse.
        return text

    def validate(self, params: dict = None) -> None:
        if "entity_mapping" not in params:
            raise ValueError("An input Dict called `entity_mapping` is required.")
        if "entity_type" not in params:
            raise ValueError("An entity_type param is required.")

    def operator_name(self) -> str:
        return "entity_counter_deanonymizer"

    def operator_type(self) -> OperatorType:
        return OperatorType.Deanonymize
