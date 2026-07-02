# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
"""A lightweight, dependency-light detection engine (regex + checksums, no spaCy).

Detection here is plain regex plus validators (Luhn, IPv4 range, IBAN mod-97)
and a small name heuristic — it does NOT do named-entity recognition, so it has
lower recall than the Presidio engine (it will not catch names/locations/orgs
that lack a distinctive pattern). The *anonymization* uses the real Presidio
``AnonymizerEngine`` + our reversible ``InstanceCounter`` operator, so the
placeholders / mapping / round-trip are identical to the Presidio engine.

Enable with ``CUSTODIO_ENGINE=regex`` when you cannot install spaCy or want a
fast, minimal footprint. ``CUSTODIO_ENGINE=presidio`` (the default) is
recommended for the best coverage.

Interface matches :meth:`custodio.pii.PIIEngine.process_span`.
"""

from __future__ import annotations

import re

from presidio_anonymizer import AnonymizerEngine, OperatorConfig
from presidio_anonymizer.entities import RecognizerResult

from .config import Settings
from .operators import InstanceCounterAnonymizer
from .pii import EntityHit, EntityMapping, PossibleMiss, mask_value


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum, as Presidio's real CREDIT_CARD recognizer requires."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _valid_ipv4(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _iban_ok(text: str) -> bool:
    """ISO 13616 mod-97 check on a whitespace-stripped IBAN."""
    compact = text.replace(" ", "").upper()
    if len(compact) < 15 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    if not digits.isdigit():
        return False
    return int(digits) % 97 == 1


def _cc_ok(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


# (entity_type, compiled regex, score, validator). Patterns mirror Presidio's
# predefined recognizers; validators cut the false positives/negatives a bare
# regex would produce.
_PATTERNS: list[tuple[str, re.Pattern[str], float, object]] = [
    ("EMAIL_ADDRESS", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), 1.0, None),
    # Candidate 13-19 digit runs (optionally space/dash grouped), gated by Luhn.
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\d?\b"), 0.9, _cc_ok),
    ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), 0.85, None),
    ("IP_ADDRESS", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0.9, _valid_ipv4),
    # Allow the common 4-char-group spacing; validate the mod-97 checksum.
    ("IBAN_CODE", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b"), 0.9, _iban_ok),
    # Require a +country code, parenthesized area code, or separators between
    # groups so bare 6-7 digit IDs don't match. The (?<!\d[.\-]) / (?![.\-]\d)
    # guards stop it from chewing a sub-run of a longer dotted number (IP /
    # version / IBAN-ish string like 999.999.999.999).
    ("PHONE_NUMBER", re.compile(r"(?<!\w)(?<!\d[.\-])(?:\+\d{1,3}[ .\-]?)?(?:\(\d{2,4}\)[ .\-]?|\d{2,4}[ .\-])\d{2,4}[ .\-]?\d{2,4}(?!\w)(?![.\-]\d)"), 0.75, None),
    ("URL", re.compile(r"\bhttps?://[^\s\"'<>]+"), 0.6, None),
]

# Multi-word Capitalized sequences look like names (PERSON). This is a crude
# heuristic — the Presidio engine uses a NER model for real name detection.
_PERSON = re.compile(r"\b(?:[A-Z][a-z]+)(?:\s+[A-Z][a-z]+)+\b")
# Single capitalized word mid-sentence -> low-confidence "possible miss".
_MAYBE = re.compile(r"(?<=[a-z]\s)[A-Z][a-z]{2,}\b")

# Common capitalized words that are NOT names (reduce false positives).
_STOP = {
    "The", "This", "That", "Please", "Hello", "Hi", "Dear", "Thanks", "Thank",
    "Best", "Regards", "Send", "Email", "Call", "Card", "Number", "Name",
}


class RegexEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.anonymizer = AnonymizerEngine()
        self.anonymizer.add_anonymizer(InstanceCounterAnonymizer)

    def _filter(self, etype: str) -> bool:
        s = self.settings
        if etype in s.denied_entities:
            return False
        if s.allowed_entities is not None and etype not in s.allowed_entities:
            return False
        return True

    def _detect(self, text: str) -> list[RecognizerResult]:
        spans: list[RecognizerResult] = []
        for etype, pattern, score, validator in _PATTERNS:
            for m in pattern.finditer(text):
                if not m.group().strip():
                    continue
                if validator is not None and not validator(m.group()):
                    continue
                spans.append(RecognizerResult(etype, m.start(), m.end(), score))
        for m in _PERSON.finditer(text):
            if m.group().split()[0] not in _STOP:
                spans.append(RecognizerResult("PERSON", m.start(), m.end(), 0.85))
        for m in _MAYBE.finditer(text):
            if m.group() not in _STOP:
                spans.append(RecognizerResult("PERSON", m.start(), m.end(), 0.4))
        return self._resolve_overlaps([s for s in spans if self._filter(s.entity_type)])

    @staticmethod
    def _resolve_overlaps(spans: list[RecognizerResult]) -> list[RecognizerResult]:
        """Greedily keep the highest-scoring span from each overlapping group."""
        chosen: list[RecognizerResult] = []
        for s in sorted(spans, key=lambda r: (-r.score, r.start)):
            if all(not (s.start < c.end and c.start < s.end) for c in chosen):
                chosen.append(s)
        chosen.sort(key=lambda r: r.start)
        return chosen

    def process_span(
        self,
        text: str,
        entity_mapping: EntityMapping,
        reserved: set[str] | None = None,
    ) -> tuple[str, list[EntityHit], list[PossibleMiss]]:
        s = self.settings
        if not text or not text.strip():
            return text, [], []

        results = self._detect(text)
        to_anon = [r for r in results if r.score >= s.score_threshold]
        misses = [
            PossibleMiss(r.entity_type, mask_value(text[r.start : r.end]), round(r.score, 3))
            for r in results
            if s.shadow_threshold <= r.score < s.score_threshold
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
            if placeholder:
                hits.append(
                    EntityHit(
                        entity_type=r.entity_type,
                        placeholder=placeholder,
                        original_masked=mask_value(original),
                        score=round(r.score, 3),
                        original=original if s.store_full_pii else None,
                    )
                )
        return anonymized.text, hits, misses
