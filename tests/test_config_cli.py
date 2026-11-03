"""aiproxy config: get/set/unset, YAML value parsing, validation, comments."""

from __future__ import annotations

import tempfile
from types import SimpleNamespace

from aiproxy import config_cli

_SRC = """\
# top comment
listen:
  host: 127.0.0.1
  port: 8080
upstreams:
  anthropic:
    base_url: https://api.anthropic.com
pipeline: [anonymizer]
llm:
  model: qwen2.5:3b-instruct   # inline comment
store:
  path: ./map.db
  encrypt: false
anonymizer:
  entities:
    PERSON: anonymize
"""


def _file():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(_SRC); f.close()
    return f.name


def _ns(cfgpath, action, **kw):
    return SimpleNamespace(cmd="config", config=cfgpath, action=action, **kw)


def _read(path):
    with open(path) as f:
        return f.read()


def test_set_scalar_coerces_type_and_keeps_comments():
    p = _file()
    assert config_cli.main(_ns(p, "set", path="listen.port", value="9000")) == 0
    txt = _read(p)
    assert "port: 9000" in txt
    assert "# top comment" in txt and "# inline comment" in txt
    # value is an int, not a string
    assert config_cli._get(config_cli._load(p), "listen.port") == 9000


def test_set_list_and_nested_dict_key():
    p = _file()
    config_cli.main(_ns(p, "set", path="llm.models",
                        value="[qwen2.5:3b-instruct, llama3.2:latest]"))
    config_cli.main(_ns(p, "set", path="anonymizer.entities.ORG", value="anonymize"))
    data = config_cli._load(p)
    assert list(config_cli._get(data, "llm.models")) == \
        ["qwen2.5:3b-instruct", "llama3.2:latest"]
    assert config_cli._get(data, "anonymizer.entities.ORG") == "anonymize"


def test_unset():
    p = _file()
    config_cli.main(_ns(p, "set", path="llm.models", value="[a, b]"))
    assert config_cli.main(_ns(p, "unset", path="llm.models")) == 0
    data = config_cli._load(p)
    try:
        config_cli._get(data, "llm.models")
        assert False, "should be gone"
    except KeyError:
        pass


def test_removing_required_key_is_rejected():
    p = _file()
    before = _read(p)
    rc = config_cli.main(_ns(p, "unset", path="store"))
    assert rc == 1
    assert _read(p) == before  # unchanged on rejection
