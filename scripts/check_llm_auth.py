#!/usr/bin/env python3
"""Fail-fast LLM auth check for grok-backed agents.

Prints one JSON object. Exit 0 if usable; 2 if AUTH_NEEDED.
Never prints credential contents.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def inspect_auth(path: Path) -> dict:
    if not path.is_file():
        return {"path": str(path), "exists": False, "usable": False, "reason": "missing"}
    size = path.stat().st_size
    if size < 32:
        return {
            "path": str(path),
            "exists": True,
            "usable": False,
            "size": size,
            "reason": "empty",
        }
    # Grok auth.json is a credential blob. Never print values.
    info = {"path": str(path), "exists": True, "usable": True, "size": size}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return info  # unreadable to this uid: size is the only signal
    if not isinstance(data, dict) or not data:
        return info
    inner = next(iter(data.values())) if data else None
    if isinstance(inner, dict) and inner.get("expires_at"):
        info["expires_at"] = str(inner.get("expires_at"))
        try:
            from datetime import datetime, timezone
            raw = str(inner["expires_at"]).strip()
            exp = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp:
                info["usable"] = False
                info["reason"] = "expired"
        except (TypeError, ValueError):
            pass
    return info


def homes_from_config(config: Path, names: list[str]) -> list[tuple[str, Path, str]]:
    cfg = json.loads(config.read_text())
    agents = cfg.get("agents") or {}
    out = []
    for name in names:
        info = agents.get(name) or {}
        home = info.get("home")
        backend = str(info.get("backend") or "grok")
        if not home:
            continue
        out.append((name, Path(home), backend))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Check grok auth.json is present and non-empty")
    ap.add_argument("--home", help="agent home (skip config)")
    ap.add_argument("--agent", default="agent")
    ap.add_argument("--config", help="agents.json for named agents")
    ap.add_argument("names", nargs="*")
    args = ap.parse_args()

    targets: list[tuple[str, Path, str]] = []
    if args.home:
        targets.append((args.agent, Path(args.home), "grok"))
    elif args.config:
        names = args.names
        if not names:
            cfg = json.loads(Path(args.config).read_text())
            names = list((cfg.get("agents") or {}).keys())
        targets = homes_from_config(Path(args.config), names)
    else:
        print(json.dumps({"ok": False, "error": "need --home or --config"}), file=sys.stderr)
        return 2

    needed = []
    rows = []
    for name, home, backend in targets:
        if backend == "claude":
            rows.append({"agent": name, "backend": "claude", "skipped": True})
            continue
        info = inspect_auth(home / ".grok" / "auth.json")
        info["agent"] = name
        info["backend"] = backend
        rows.append(info)
        if not info.get("usable"):
            needed.append(name)

    reasons = sorted({str(r.get("reason") or "unusable") for r in rows if not r.get("usable") and not r.get("skipped")})
    expired = "expired" in reasons
    hint = None
    if needed:
        hint = (
            "Grok login on the holder, then Sync LLM Auth (token expired)"
            if expired
            else "Sync LLM Auth (holder → citizen homes), then retry start"
        )
    payload = {
        "ok": not needed,
        "authNeeded": bool(needed),
        "agents": needed,
        "rows": rows,
        "hint": hint,
    }
    print(json.dumps(payload))
    if needed:
        print(
            "AUTH_NEEDED: "
            + ", ".join(needed)
            + " ("
            + ", ".join(reasons)
            + "). "
            + hint,
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
