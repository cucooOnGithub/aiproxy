"""The deterministic regex backstop in the image/PDF path: emails, phones,
IBANs, API keys and credit cards in OCR'd text are masked even when the NER
model misses them and even when OCR splits a value across word boxes."""

from __future__ import annotations

from aiproxy.processors.image_redactor import ImageRedactor
from aiproxy.processors.secrets_regex import _accept


def _spans(words):
    """Reproduce the join + per-word char spans the redactor builds."""
    spans, pos, pieces = {}, 0, []
    for i, w in enumerate(words):
        start = pos
        pieces.append(w)
        pos += len(w)
        spans[i] = (start, pos)
        pos += 1
    return " ".join(pieces), spans


def _red():
    return ImageRedactor({})


def test_email_split_across_boxes_is_flagged():
    # OCR often splits an email around the '@'.
    words = ["Email:", "theo.marchetti", "@", "brambleworth-ventures.example", "ok"]
    joined, spans = _spans(words)
    idx = _red()._regex_flagged_indices(joined, spans, words)
    assert 1 in idx and 2 in idx and 3 in idx   # the email parts
    assert 0 not in idx and 4 not in idx         # "Email:" and "ok" untouched


def test_spaced_phone_flags_all_groups():
    words = ["Phone:", "+351", "912", "884", "573", "end"]
    joined, spans = _spans(words)
    idx = _red()._regex_flagged_indices(joined, spans)
    assert {1, 2, 3, 4} <= idx
    assert 0 not in idx and 5 not in idx


def test_credit_card_flagged_phone_capped():
    # 16-digit Luhn-valid card: too many digits to be a PHONE, caught as CARD.
    words = ["Card", "4111", "1111", "1111", "1111"]
    joined, spans = _spans(words)
    idx = _red()._regex_flagged_indices(joined, spans)
    assert {1, 2, 3, 4} <= idx


def test_non_card_long_number_not_flagged():
    # Fails Luhn and isn't a plausible phone -> not masked as a card.
    words = ["ref", "1234", "5678", "9012", "3456"]
    joined, spans = _spans(words)
    idx = _red()._regex_flagged_indices(joined, spans)
    # none of the number words should be card-flagged (Luhn rejects)
    assert not ({1, 2, 3, 4} <= idx)


def test_luhn_accept():
    assert _accept("CREDITCARD", "4111 1111 1111 1111")      # valid Luhn
    assert not _accept("CREDITCARD", "1234 5678 9012 3456")  # invalid Luhn
    assert not _accept("CREDITCARD", "4111 1111")            # too short


def test_disabling_patterns():
    r = ImageRedactor({"ocr_regex": ["PHONE"]})
    words = ["m.villanueva@northgate-legal.example"]
    joined, spans = _spans(words)
    assert r._regex_flagged_indices(joined, spans) == set()  # EMAIL not enabled
