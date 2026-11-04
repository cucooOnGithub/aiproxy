"""Gazetteer must catch names joined by dots/underscores (incl. inside paths)
and an initial before a known surname, not just capitalized whitespace runs."""
from __future__ import annotations

import tempfile

from aiproxy.document import Document, Part, PartKind
from aiproxy.pipeline.base import SessionContext
from aiproxy.processors.anonymizer import Anonymizer
from aiproxy.store.crypto import Cipher
from aiproxy.store.sqlite_store import SqliteStore


def _anon():
    # gazetteer with john/rivera/maria/silva; NER disabled (returns nothing).
    gaz = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    gaz.write("john\nrivera\nmaria\nsilva\n"); gaz.close()
    a = Anonymizer({"entities": {"PERSON": "anonymize"},
                    "name_gazetteer": gaz.name, "known_value_sweep": False})
    a.ner.detect = lambda _t: []   # isolate the gazetteer
    return a


def _ctx():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    return SessionContext("s", SqliteStore(db, Cipher.null()), {})


def _run(a, text):
    return a.process_request(
        Document(parts=[Part(PartKind.TEXT, text=text)]), _ctx()).parts[0].text


def test_whitespace_name_still_works():
    assert "Maria Silva" not in _run(_anon(), "hi Maria Silva")


def test_dotted_lowercase_username():
    out = _run(_anon(), "login john.rivera now")
    assert "john.rivera" not in out and "⟦PERSON" in out


def test_dotted_username_inside_path():
    out = _run(_anon(), "dir: /Users/john.rivera/AI/projects")
    assert "john.rivera" not in out
    assert "/Users/" in out and "/AI/projects" in out   # path structure kept


def test_underscore_form():
    assert "john_rivera" not in _run(_anon(), "user john_rivera here")


def test_initial_plus_surname():
    out = _run(_anon(), "signed J. Rivera today")
    assert "J. Rivera" not in out and "⟦PERSON" in out


def test_lone_initial_not_a_name():
    # A bare initial with no known surname must be left alone.
    assert _run(_anon(), "option C. is best") == "option C. is best"
