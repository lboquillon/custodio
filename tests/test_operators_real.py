"""Validate the InstanceCounter operators against the REAL Presidio engines.

Needs `presidio-anonymizer` (lightweight, no spaCy). We feed analyzer results
directly, so no NLP model is required.

Run: python tests/test_operators_real.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from presidio_anonymizer import AnonymizerEngine, DeanonymizeEngine, OperatorConfig
from presidio_anonymizer.entities import OperatorResult, RecognizerResult

from custodio.operators import InstanceCounterAnonymizer, InstanceCounterDeanonymizer
from custodio.pii import build_reverse_map, deanonymize_text


def test_real_anonymize_then_deanonymize():
    text = "My name is Jane Doe and my friend is Jane Doe's boss Bob."
    #        0123456789...        Jane Doe at 11-19; second Jane Doe; Bob
    anonymizer = AnonymizerEngine()
    anonymizer.add_anonymizer(InstanceCounterAnonymizer)

    entity_mapping = {}
    # two PERSON spans with the SAME value + one different -> tests consistency
    j1 = text.index("Jane Doe")
    j2 = text.index("Jane Doe", j1 + 1)
    b = text.index("Bob")
    results = [
        RecognizerResult("PERSON", j1, j1 + len("Jane Doe"), 0.9),
        RecognizerResult("PERSON", j2, j2 + len("Jane Doe"), 0.9),
        RecognizerResult("PERSON", b, b + len("Bob"), 0.9),
    ]

    anon = anonymizer.anonymize(
        text=text,
        analyzer_results=results,
        operators={"DEFAULT": OperatorConfig("entity_counter",
                                             {"entity_mapping": entity_mapping})},
    )

    # same value -> same placeholder; distinct value -> distinct placeholder.
    # (Presidio applies operators back-to-front, so the *index order* is not
    # left-to-right — that's cosmetic; consistency + round-trip are what matter.)
    jane_ph = entity_mapping["PERSON"]["Jane Doe"]
    bob_ph = entity_mapping["PERSON"]["Bob"]
    assert {jane_ph, bob_ph} == {"<PERSON_0>", "<PERSON_1>"}
    assert jane_ph != bob_ph
    assert anon.text.count(jane_ph) == 2  # both "Jane Doe" -> same placeholder
    assert anon.text.count(bob_ph) == 1
    assert "Jane Doe" not in anon.text and "Bob" not in anon.text

    # --- de-anonymize via the real DeanonymizeEngine ---
    deanon = DeanonymizeEngine()
    deanon.add_deanonymizer(InstanceCounterDeanonymizer)
    # locate placeholders to build OperatorResults
    entities = []
    for etype, m in entity_mapping.items():
        for original, ph in m.items():
            start = 0
            while (start := anon.text.find(ph, start)) != -1:
                entities.append(OperatorResult(start, start + len(ph), etype, original, ph))
                start += len(ph)
    restored = deanon.deanonymize(
        text=anon.text, entities=entities,
        operators={"DEFAULT": OperatorConfig("entity_counter_deanonymizer",
                                             {"entity_mapping": entity_mapping})},
    )
    assert restored.text == text

    # --- and via our lightweight regex path (used in streaming) ---
    assert deanonymize_text(anon.text, build_reverse_map(entity_mapping)) == text


if __name__ == "__main__":
    test_real_anonymize_then_deanonymize()
    print("ok: test_real_anonymize_then_deanonymize")
    print("\n1 test passed")
