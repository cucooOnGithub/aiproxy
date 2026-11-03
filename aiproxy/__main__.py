"""CLI entrypoint.

    python -m aiproxy serve   --config config.yaml
    python -m aiproxy purge   --config config.yaml --session <id>
    python -m aiproxy genkey
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aiproxy")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the proxy")
    s.add_argument("--config", default="config.yaml")

    p = sub.add_parser("purge", help="delete a session's mappings")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--session", required=True)

    sub.add_parser("genkey", help="print a new store encryption key")

    d = sub.add_parser("doctor", help="check the install is complete/runnable")
    d.add_argument("--config", default="config.yaml")

    c = sub.add_parser("config", help="view/change settings in config.yaml")
    c.add_argument("--config", default="config.yaml")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("show", help="print the whole config")
    cg = csub.add_parser("get", help="print one value")
    cg.add_argument("path")
    cse = csub.add_parser("set", help="set one value (parsed as YAML)")
    cse.add_argument("path")
    cse.add_argument("value")
    cu = csub.add_parser("unset", help="remove one key")
    cu.add_argument("path")
    csub.add_parser("edit", help="interactive wizard over common settings")

    args = ap.parse_args(argv)

    if args.cmd == "config":
        from aiproxy.config_cli import main as config_main

        return config_main(args)

    if args.cmd == "genkey":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0

    if args.cmd == "doctor":
        from aiproxy.doctor import main as doctor_main

        return doctor_main(args.config)

    from aiproxy.config.loader import load_config

    if args.cmd == "serve":
        import uvicorn

        cfg = load_config(args.config)
        from aiproxy.proxy.server import create_app

        app = create_app(cfg)
        listen = cfg["listen"]
        uvicorn.run(app, host=listen["host"], port=listen["port"])
        return 0

    if args.cmd == "purge":
        cfg = load_config(args.config)
        from aiproxy.proxy.server import _build_store

        n = _build_store(cfg).purge_session(args.session)
        print(f"purged {n} mappings for session {args.session}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
