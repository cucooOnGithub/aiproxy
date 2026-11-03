"""`aiproxy config` — view and change every setting in config.yaml.

Generic dotted-path access covers ALL settings:
    aiproxy config show
    aiproxy config get llm.model
    aiproxy config set llm.model qwen2.5:3b-instruct
    aiproxy config set listen.port 9000
    aiproxy config set llm.models '[qwen2.5:3b-instruct, llama3.2:latest]'
    aiproxy config set anonymizer.entities.PERSON anonymize
    aiproxy config unset llm.models
    aiproxy config edit            # interactive wizard over common settings

Values are parsed as YAML (so `9000`->int, `true`->bool, `null`->None,
`[a, b]`->list, `{k: v}`->dict). Comments and formatting are preserved
(ruamel.yaml round-trip). Every write is validated by loading the result
through the normal config loader; on error nothing is written.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)

# (dotted path, prompt shown, example) for the interactive wizard. The
# generic set/unset can reach anything; this just guides the common ones.
_WIZARD = [
    ("listen.host", "Bind host", "127.0.0.1"),
    ("listen.port", "Bind port", "8088"),
    ("upstreams.anthropic.base_url", "Anthropic upstream URL",
     "https://api.anthropic.com"),
    ("upstreams.openai.base_url", "OpenAI upstream URL",
     "https://api.openai.com"),
    ("llm.url", "Ollama URL", "http://localhost:11434"),
    ("llm.model", "Single NER model (leave blank if using models:)",
     "qwen2.5:3b-instruct"),
    ("llm.models", "Ensemble models as a YAML list (blank = use model:)",
     "[qwen2.5:3b-instruct, llama3.2:latest]"),
    ("llm.timeout", "NER request timeout (s)", "60"),
    ("llm.warm_timeout", "Pre-warm timeout (s)", "300"),
    ("llm.keep_alive", "Ollama keep_alive (0 to unload each call)", "0"),
    ("llm.cache_ttl", "NER cache TTL (s, null=forever)", "3600"),
    ("mode", "Mode (fail_closed | fail_open | observe). observe forwards "
     "UN-redacted and just logs - sends real data upstream!", "fail_closed"),
    ("scope.redact_system",
     "Redact the system prompt too? Claude Code system prompt + CLAUDE.md "
     "live here (true/false)", "false"),
    ("scope.skip_tags",
     "Tags whose <tag>...</tag> regions are never redacted (skills, commands, "
     "reminders), as a YAML list",
     "[system-reminder, command-name, command-args, command-message, "
     "local-command-stdout, local-command-stderr]"),
    ("scope.skip_prefixes",
     "Protect a whole text block when it STARTS WITH any of these (untagged "
     "operational blocks), as a YAML list",
     '["# Environment", "You are Claude Code", "Direct output style is active", '
     '"[SUGGESTION MODE"]'),
    ("scope.skip_regexes",
     "Protect a whole text block when any of these regexes matches it "
     "(re.search, DOTALL), as a YAML list", "[]"),
    ("image_redactor.max_pages", "Max PDF pages", "20"),
    ("image_redactor.dpi", "Scanned-page raster DPI", "200"),
    ("store.path", "Store DB path", "./map.db"),
    ("store.encrypt", "Encrypt store at rest (true/false)", "true"),
    ("archive.dir", "Redacted-document archive/cache dir (blank = off)", ""),
    ("redaction_log.path", "Redaction debug JSONL path (blank = off)", ""),
    ("redaction_log.debug",
     "Log ORIGINAL message text too - plaintext PII! (true/false)", "false"),
    ("redaction_log.log_all",
     "Log EVERY text block, not just redacted ones (true/false)", "false"),
    ("prewarm", "Pre-warm models at startup (true/false)", "true"),
]


def _load(path: str):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{path} not found (copy config.example.yaml)")
    with p.open() as f:
        return _yaml.load(f)


def _dump_str(data) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _parse_value(raw: str):
    return YAML(typ="safe").load(raw)


def _get(data, dotted: str):
    cur = data
    for key in dotted.split("."):
        if cur is None or key not in cur:
            raise KeyError(dotted)
        cur = cur[key]
    return cur


def _set(data, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = data
    for key in keys[:-1]:
        if key not in cur or not hasattr(cur[key], "__setitem__"):
            cur[key] = {}
        cur = cur[key]
    cur[keys[-1]] = value


def _unset(data, dotted: str) -> None:
    keys = dotted.split(".")
    cur = data
    for key in keys[:-1]:
        cur = cur[key]
    del cur[keys[-1]]


def _validate(data) -> None:
    """Round-trip through the real loader so a bad edit is rejected."""
    import tempfile

    from aiproxy.config.loader import load_config
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(_dump_str(data))
        tmp = f.name
    load_config(tmp)  # raises on missing required keys / bad YAML


def _write(path: str, data) -> None:
    _validate(data)
    with open(path, "w") as f:
        _yaml.dump(data, f)


def main(args) -> int:
    action = args.action
    path = args.config

    if action == "show":
        print(_dump_str(_load(path)), end="")
        return 0

    if action == "get":
        try:
            val = _get(_load(path), args.path)
        except KeyError:
            print(f"not set: {args.path}", file=sys.stderr)
            return 1
        if isinstance(val, (list, dict)):
            print(_dump_str(val), end="")
        else:
            print(val)
        return 0

    if action == "set":
        data = _load(path)
        _set(data, args.path, _parse_value(args.value))
        try:
            _write(path, data)
        except Exception as e:  # noqa: BLE001
            print(f"rejected (not written): {e}", file=sys.stderr)
            return 1
        print(f"set {args.path} = {args.value}")
        return 0

    if action == "unset":
        data = _load(path)
        try:
            _unset(data, args.path)
        except (KeyError, TypeError):
            print(f"not set: {args.path}", file=sys.stderr)
            return 1
        try:
            _write(path, data)
        except Exception as e:  # noqa: BLE001
            print(f"rejected (not written): {e}", file=sys.stderr)
            return 1
        print(f"unset {args.path}")
        return 0

    if action == "edit":
        return _interactive(path)

    print("unknown action", file=sys.stderr)
    return 2


def _interactive(path: str) -> int:
    data = _load(path)
    print(f"Editing {path} — Enter keeps the current value.\n")
    for dotted, label, example in _WIZARD:
        try:
            current = _get(data, dotted)
            cur_s = _dump_str(current).strip() if (isinstance(current, (list, dict))) \
                else str(current)
        except KeyError:
            cur_s = "(unset)"
        try:
            raw = input(f"{label}\n  {dotted} [{cur_s}] (e.g. {example}): ").strip()
        except EOFError:
            break
        if raw == "":
            continue
        if raw.lower() in ("-", "unset"):
            try:
                _unset(data, dotted)
            except (KeyError, TypeError):
                pass
            continue
        _set(data, dotted, _parse_value(raw))
    try:
        _write(path, data)
    except Exception as e:  # noqa: BLE001
        print(f"\nrejected (not written): {e}", file=sys.stderr)
        return 1
    print(f"\nSaved {path}.")
    return 0
