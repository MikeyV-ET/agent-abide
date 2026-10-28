#!/usr/bin/env python3
"""Grok-backend history audit: updates.jsonl vs house hot.jsonl.

Walks the current session flight recorder (user_message_chunk /
agent_message_chunk) and compares coalesced text to asdaaas
history/hot.jsonl class=message. Does not parse Claude jsonl.

Usage:
  PYTHONPATH=core python3 scripts/audit_grok_history.py --agent Trip-G
  python3 scripts/audit_grok_history.py --updates PATH --hot PATH
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote


def _text_from_update(update: dict) -> str:
    content = update.get("content", {})
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, str):
        return content
    return ""


def iter_updates_utterances(path: Path):
    """Yield (role, text) coalescing consecutive same-role chunks."""
    role = None
    buf: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            update = (obj.get("params") or {}).get("update") or {}
            su = update.get("sessionUpdate") or ""
            if su == "user_message_chunk":
                r, t = "user", _text_from_update(update)
            elif su == "agent_message_chunk":
                r, t = "assistant", _text_from_update(update)
            else:
                continue
            if not t:
                continue
            if role is None:
                role, buf = r, [t]
            elif r == role:
                buf.append(t)
            else:
                yield role, "".join(buf)
                role, buf = r, [t]
    if role is not None:
        yield role, "".join(buf)


def _hot_text(body) -> str:
    if isinstance(body, dict):
        return str(body.get("text") or body.get("content") or "")
    if isinstance(body, str):
        return body
    return ""


def iter_hot_utterances(path: Path):
    role = None
    buf: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("class") != "message":
                continue
            r = obj.get("role")
            if r not in ("user", "assistant"):
                continue
            t = _hot_text(obj.get("body"))
            if not t:
                continue
            if role is None:
                role, buf = r, [t]
            elif r == role:
                buf.append(t)
            else:
                yield role, "".join(buf)
                role, buf = r, [t]
    if role is not None:
        yield role, "".join(buf)


def summarize(utts):
    n_u = n_a = c_u = c_a = 0
    for r, t in utts:
        if r == "user":
            n_u += 1
            c_u += len(t)
        else:
            n_a += 1
            c_a += len(t)
    return {"n_user": n_u, "n_asst": n_a, "chars_user": c_u, "chars_asst": c_a}


def concat_role(utts, role: str) -> str:
    return "".join(t for r, t in utts if r == role)


def resolve_paths(agent: str):
    roster = Path.home() / "agents" / "config" / "agents.json"
    data = json.loads(roster.read_text())
    home = session = None

    def walk(x):
        nonlocal home, session
        if isinstance(x, dict):
            name = str(x.get("name") or "")
            if name.lower() == agent.lower() or str(x.get("home") or "").rstrip("/").endswith("/" + agent):
                home = x.get("home")
                session = x.get("session")
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    if not home:
        home = str(Path.home() / "agents" / agent)
    health = Path(home) / "asdaaas" / "health.json"
    if health.is_file():
        try:
            h = json.loads(health.read_text())
            session = h.get("session_id") or h.get("session") or session
        except Exception:
            pass
    if not session:
        raise SystemExit(f"no session id for {agent}")
    encoded = quote(str(home), safe="")
    updates = Path.home() / ".grok" / "sessions" / encoded / session / "updates.jsonl"
    hot = Path(home) / "asdaaas" / "history" / "hot.jsonl"
    return updates, hot, session


def audit(updates: Path, hot: Path) -> dict:
    if not updates.is_file():
        raise SystemExit(f"missing updates.jsonl: {updates}")
    if not hot.is_file():
        raise SystemExit(f"missing hot.jsonl: {hot}")
    u_list = list(iter_updates_utterances(updates))
    h_list = list(iter_hot_utterances(hot))
    su, sh = summarize(u_list), summarize(h_list)
    u_as, h_as = concat_role(u_list, "assistant"), concat_role(h_list, "assistant")
    u_us, h_us = concat_role(u_list, "user"), concat_role(h_list, "user")

    def side_ok(src_n, house_n, src_s, house_s):
        if src_n == 0 and house_n == 0:
            return True
        r = abs(src_n - house_n) / max(src_n, house_n, 1)
        if r <= 0.15:
            return True
        # house subset of grok (ingest drop) only if it still has half the chars
        if house_s and src_s and house_s in src_s and house_n >= 0.5 * src_n:
            return True
        if src_s and house_s and src_s in house_s and src_n >= 0.5 * house_n:
            return True
        return False

    asst_ok = side_ok(su["chars_asst"], sh["chars_asst"], u_as, h_as)
    user_ok = side_ok(su["chars_user"], sh["chars_user"], u_us, h_us)
    return {
        "updates": str(updates),
        "hot_path": str(hot),
        "updates_bytes": updates.stat().st_size,
        "hot_bytes": hot.stat().st_size,
        "grok": su,
        "house": sh,
        "asst_consistent": asst_ok,
        "user_consistent": user_ok,
        "ok": asst_ok and user_ok,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", help="Roster name (resolves session + home)")
    ap.add_argument("--updates", type=Path)
    ap.add_argument("--hot", type=Path)
    args = ap.parse_args()
    if args.agent:
        updates, hot, sid = resolve_paths(args.agent)
        print(f"agent={args.agent} session={sid}", file=sys.stderr)
    else:
        if not args.updates or not args.hot:
            ap.error("need --agent or both --updates and --hot")
        updates, hot = args.updates, args.hot
    report = audit(updates, hot)
    json.dump({k: v for k, v in report.items() if k != "ok"}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    print("PASS" if report["ok"] else "FAIL", file=sys.stderr)
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
