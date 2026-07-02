"""Tests for the pure-Python helpers in pii.py (no Presidio needed)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custodio.pii import build_reverse_map, deanonymize_text, mask_value  # noqa: E402


def test_mask_value():
    assert mask_value("a") == "•"
    assert mask_value("jane@acme.com").startswith("ja")
    assert mask_value("jane@acme.com").endswith("om")
    assert "•" in mask_value("jane@acme.com")


def test_build_reverse_map():
    mapping = {
        "PERSON": {"Jane Doe": "<PERSON_0>", "Bob": "<PERSON_1>"},
        "EMAIL_ADDRESS": {"jane@acme.com": "<EMAIL_ADDRESS_0>"},
    }
    rev = build_reverse_map(mapping)
    assert rev["<PERSON_0>"] == "Jane Doe"
    assert rev["<PERSON_1>"] == "Bob"
    assert rev["<EMAIL_ADDRESS_0>"] == "jane@acme.com"


def test_deanonymize_text_longest_first():
    rev = {"<PERSON_1>": "Bob", "<PERSON_10>": "Alice"}
    assert deanonymize_text("<PERSON_10> & <PERSON_1>", rev) == "Alice & Bob"


def test_deanonymize_text_unknown_placeholder_untouched():
    rev = {"<PERSON_0>": "Jane"}
    assert deanonymize_text("hi <PERSON_0> and <PERSON_9>", rev) == "hi Jane and <PERSON_9>"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print(f"\n{len(fns)} tests passed")
