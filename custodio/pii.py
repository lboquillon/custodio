# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""Presidio wrapper: detect PII, anonymize reversibly, de-anonymize.

Everything that needs the Presidio libraries lives here and is imported lazily
(inside ``PIIEngine.__init__``) so the rest of Custodio imports cleanly even
when Presidio / spaCy are not installed (e.g. for unit tests of the proxy
plumbing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Settings

EntityMapping = dict[str, dict[str, str]]  # {entity_type: {original: placeholder}}


def mask_value(value: str) -> str:
    """Redact a raw PII value for safe display in the audit log."""
    if len(value) <= 2:
        return "•" * len(value)
    if len(value) <= 6:
        return value[0] + "•" * (len(value) - 1)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]


def build_reverse_map(entity_mapping: EntityMapping) -> dict[str, str]:
    """{placeholder: original} across all entity types."""
    reverse: dict[str, str] = {}
    for per_type in entity_mapping.values():
        for original, placeholder in per_type.items():
            reverse[placeholder] = original
    return reverse


def deanonymize_text(text: str, reverse_map: dict[str, str]) -> str:
    """Replace every known placeholder in ``text`` with its original value."""
    if not reverse_map or not text:
        return text
    # Longest placeholders first so <PERSON_10> wins over <PERSON_1>.
    placeholders = sorted(reverse_map, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(p) for p in placeholders))
    return pattern.sub(lambda m: reverse_map[m.group(0)], text)


@dataclass
class EntityHit:
    """A PII span that WAS anonymized."""

    entity_type: str
    placeholder: str
    original_masked: str
    score: float
    original: str | None = None  # only populated when store_full_pii=True


@dataclass
class PossibleMiss:
    """A low-confidence span that was NOT anonymized (shadow pass)."""

    entity_type: str
    text_masked: str
    score: float


class PIIEngine:
    """Thin, opinionated facade over Presidio's three engines."""

    def __init__(self, settings: Settings):
        # --- lazy, heavy imports -------------------------------------------
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_anonymizer import AnonymizerEngine

        from .operators import InstanceCounterAnonymizer

        self.settings = settings

        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": settings.language, "model_name": settings.spacy_model}
                ],
            }
        ).create_engine()

        self.analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine, supported_languages=[settings.language]
        )
        self.anonymizer = AnonymizerEngine()
        self.anonymizer.add_anonymizer(InstanceCounterAnonymizer)

    # ------------------------------------------------------------------ #

    def _entity_filter(self, entity_type: str) -> bool:
        s = self.settings
        if entity_type in s.denied_entities:
            return False
        if s.allowed_entities is not None and entity_type not in s.allowed_entities:
            return False
        return True

    def anonymize_span(
        self,
        text: str,
        entity_mapping: EntityMapping,
        reserved: set[str] | None = None,
    ) -> tuple[str, list[EntityHit]]:
        """Anonymize one text span, updating the shared ``entity_mapping``.

        Returns the anonymized text and the list of entities replaced in it.
        """
        from presidio_anonymizer.entities import OperatorConfig

        if not text or not text.strip():
            return text, []

        results = self.analyzer.analyze(
            text=text,
            language=self.settings.language,
            score_threshold=self.settings.score_threshold,
        )
        results = [r for r in results if self._entity_filter(r.entity_type)]
        if not results:
            return text, []

        anonymized = self.anonymizer.anonymize(
            text=text,
            analyzer_results=results,
            operators={
                "DEFAULT": OperatorConfig(
                    "entity_counter",
                    {"entity_mapping": entity_mapping, "reserved": reserved or set()},
                )
            },
        )

        hits: list[EntityHit] = []
        for r in results:
            original = text[r.start : r.end]
            placeholder = entity_mapping.get(r.entity_type, {}).get(original, "")
            if not placeholder:
                continue
            hits.append(
                EntityHit(
                    entity_type=r.entity_type,
                    placeholder=placeholder,
                    original_masked=mask_value(original),
                    score=round(float(r.score), 3),
                    original=original if self.settings.store_full_pii else None,
                )
            )
        return anonymized.text, hits

    def process_span(
        self,
        text: str,
        entity_mapping: EntityMapping,
        reserved: set[str] | None = None,
    ) -> tuple[str, list[EntityHit], list[PossibleMiss]]:
        """Detect, anonymize, and shadow-scan a span in a single analyzer pass.

        Returns ``(anonymized_text, hits, possible_misses)``. This is the method
        the proxy uses — one ``analyze`` call at the lower shadow threshold, then
        partition results into "anonymize" (>= score_threshold) and "flag as a
        possible miss" (below it).
        """
        from presidio_anonymizer.entities import OperatorConfig

        s = self.settings
        if not text or not text.strip():
            return text, [], []

        floor = min(s.shadow_threshold, s.score_threshold)
        results = [
            r
            for r in self.analyzer.analyze(text=text, language=s.language, score_threshold=floor)
            if self._entity_filter(r.entity_type)
        ]

        to_anon = [r for r in results if r.score >= s.score_threshold]
        misses = [
            PossibleMiss(
                entity_type=r.entity_type,
                text_masked=mask_value(text[r.start : r.end]),
                score=round(float(r.score), 3),
            )
            for r in results
            if r.score < s.score_threshold
        ]

        if not to_anon:
            return text, [], misses

        anonymized = self.anonymizer.anonymize(
            text=text,
            analyzer_results=to_anon,
            operators={
                "DEFAULT": OperatorConfig(
                    "entity_counter",
                    {"entity_mapping": entity_mapping, "reserved": reserved or set()},
                )
            },
        )

        hits: list[EntityHit] = []
        for r in to_anon:
            original = text[r.start : r.end]
            placeholder = entity_mapping.get(r.entity_type, {}).get(original, "")
            if not placeholder:
                continue
            hits.append(
                EntityHit(
                    entity_type=r.entity_type,
                    placeholder=placeholder,
                    original_masked=mask_value(original),
                    score=round(float(r.score), 3),
                    original=original if s.store_full_pii else None,
                )
            )
        return anonymized.text, hits, misses

    def shadow_scan(self, text: str) -> list[PossibleMiss]:
        """Low-threshold pass: surface likely PII that fell below the bar.

        Returns spans detected in [shadow_threshold, score_threshold) — i.e.
        things a human might consider PII that Custodio did NOT anonymize.
        """
        s = self.settings
        if s.shadow_threshold >= s.score_threshold or not text.strip():
            return []
        results = self.analyzer.analyze(
            text=text, language=s.language, score_threshold=s.shadow_threshold
        )
        misses: list[PossibleMiss] = []
        for r in results:
            if r.score >= s.score_threshold:
                continue  # this one WAS anonymized
            if not self._entity_filter(r.entity_type):
                continue
            misses.append(
                PossibleMiss(
                    entity_type=r.entity_type,
                    text_masked=mask_value(text[r.start : r.end]),
                    score=round(float(r.score), 3),
                )
            )
        return misses
