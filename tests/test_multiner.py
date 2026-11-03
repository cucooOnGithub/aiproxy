"""MultiNER unions entities across backends; build_ner selects single vs
ensemble from config; keep_alive is plumbed through."""

from __future__ import annotations

from aiproxy.llm.ollama_ner import Entity, MultiNER, OllamaNER, build_ner


class _Fake:
    def __init__(self, model, ents):
        self.model = model
        self._ents = ents
        self.warmed = False

    def warm(self):
        self.warmed = True

    def detect(self, _text):
        return list(self._ents)


def test_union_merges_and_dedupes():
    a = _Fake("m1", [Entity("John Smith", "PERSON")])
    b = _Fake("m2", [Entity("John Smith", "PERSON"), Entity("Globex", "ORG")])
    out = {(e.type, e.text) for e in MultiNER([a, b]).detect("x")}
    assert out == {("PERSON", "John Smith"), ("ORG", "Globex")}


def test_non_other_type_wins():
    a = _Fake("m1", [Entity("acme", "OTHER")])
    b = _Fake("m2", [Entity("acme", "ORG")])
    out = MultiNER([a, b]).detect("x")
    assert len(out) == 1 and out[0].type == "ORG"


def test_warm_all():
    a, b = _Fake("m1", []), _Fake("m2", [])
    MultiNER([a, b]).warm()
    assert a.warmed and b.warmed


def test_build_ner_single_vs_ensemble():
    single = build_ner({"model": "llama3.2:latest"})
    assert isinstance(single, OllamaNER) and single.model == "llama3.2:latest"

    multi = build_ner({"models": ["a", "b"], "keep_alive": 0})
    assert isinstance(multi, MultiNER)
    assert [x.model for x in multi.backends] == ["a", "b"]
    assert all(x.keep_alive == 0 for x in multi.backends)
