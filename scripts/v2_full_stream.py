#!/usr/bin/env python3
"""CLI for V2 history: aa.stream hot tip + L1 seal/prune."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from full_stream import (  # noqa: E402
    HOT_KEEP_BYTES,
    HOT_MAX_BYTES,
    HOT_NAME,
    agent_history_dir,
    resolve_history_dir,
    chunks_covering_ts,
    ensure_layout,
    find_live_updates,
    open_chunk_lines,
    plan_prune_hot,
    prune_hot,
    read_manifest,
    resolve_agent_home,
    seal_byte_range,
    verify_chunk,
)
from aa_stream import (  # noqa: E402
    ensure_aa_stream_layout,
    hot_path,
    is_backend_native_path,
    read_hot_meta,
    tail_grok_once,
)


def _home(args) -> Path:
    if args.agent:
        return resolve_agent_home(args.agent)
    return Path(args.home)


def cmd_init(args):
    home = _home(args)
    agent = args.agent or home.name
    fs = ensure_aa_stream_layout(agent_history_dir(home), agent)
    print(
        json.dumps(
            {
                "status": "ok",
                "history": str(fs),
                "hot": str(hot_path(fs)),
                "meta": read_hot_meta(fs),
            },
            indent=2,
            default=str,
        )
    )


def cmd_tail_grok(args):
    home = _home(args)
    agent = args.agent or home.name
    result = tail_grok_once(
        home,
        agent,
        session_id=args.session_id,
        source=Path(args.source) if args.source else None,
        max_lines=args.max_lines,
        max_bytes=int(args.max_mib * 1024 * 1024) if args.max_mib else None,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") == "ok" else 2


def cmd_seal(args):
    home = _home(args)
    agent = args.agent or home.name
    fs = ensure_aa_stream_layout(resolve_history_dir(home), agent)
    if args.source:
        src = Path(args.source)
    else:
        # default: AA hot tip
        src = hot_path(fs)
    if not src.exists():
        print(f"source not found: {src}", file=sys.stderr)
        return 2
    start = args.start or 0
    end = args.end
    if args.mib is not None:
        end = start + int(args.mib * 1024 * 1024)
        end = min(end, src.stat().st_size)
    rec = seal_byte_range(
        src,
        fs,
        start=start,
        end=end,
        agent=agent,
        session_id=args.session_id,
        source_label=args.label or "seal",
        zstd_level=args.level,
    )
    # mark format on manifest via extra — seal already wrote; ok
    print(json.dumps(json.loads(rec.to_json()), indent=2))
    return 0


def cmd_prune_hot(args):
    home = _home(args)
    agent = args.agent or home.name
    fs = ensure_aa_stream_layout(resolve_history_dir(home), agent)
    if args.source:
        hot = Path(args.source)
    else:
        hot = hot_path(fs)
    if not hot.exists():
        print(f"hot not found: {hot}", file=sys.stderr)
        return 2
    if is_backend_native_path(hot):
        print(
            json.dumps(
                {
                    "status": "refused",
                    "error": "backend-native path; prune AA history/hot.jsonl only",
                    "path": str(hot),
                },
                indent=2,
            )
        )
        return 3
    max_b = int(args.max_mib * 1024 * 1024)
    keep_b = int(args.keep_mib * 1024 * 1024)
    if args.apply:
        try:
            result = prune_hot(
                hot,
                fs,
                apply=True,
                agent=agent,
                session_id=args.session_id,
                max_bytes=max_b,
                keep_bytes=keep_b,
                zstd_level=args.level,
            )
        except ValueError as e:
            print(json.dumps({"status": "refused", "error": str(e)}, indent=2))
            return 3
    else:
        plan = plan_prune_hot(hot, max_bytes=max_b, keep_bytes=keep_b)
        result = {
            "status": "dry_run",
            "hot": str(hot),
            "history": str(fs),
            "aa_owned": not is_backend_native_path(hot),
            **plan,
        }
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_list(args):
    home = _home(args)
    fs = resolve_history_dir(home)
    ensure_layout(fs)
    recs = read_manifest(fs)
    hp = hot_path(fs)
    meta = read_hot_meta(fs) if (fs / "hot.meta.json").exists() else {}
    total_plain = sum(r.plain_bytes for r in recs)
    total_z = sum(r.zstd_bytes for r in recs)
    print(
        json.dumps(
            {
                "history": str(fs),
                "hot": str(hp),
                "hot_bytes": hp.stat().st_size if hp.exists() else 0,
                "hot_meta": meta,
                "n_chunks": len(recs),
                "plain_mib": round(total_plain / 1024**2, 2),
                "zstd_mib": round(total_z / 1024**2, 2),
                "chunks": [json.loads(r.to_json()) for r in recs],
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_verify(args):
    home = _home(args)
    fs = resolve_history_dir(home)
    recs = read_manifest(fs)
    bad = []
    for r in recs:
        ok = verify_chunk(fs, r)
        if not ok:
            bad.append(r.chunk_id)
        print(f"{'OK' if ok else 'FAIL'}  {r.chunk_id}  {r.plain_bytes}B → {r.zstd_bytes}B")
    return 1 if bad else 0


def cmd_cat_range(args):
    home = _home(args)
    fs = resolve_history_dir(home)
    recs = chunks_covering_ts(read_manifest(fs), args.t0, args.t1)
    for r in recs:
        for line in open_chunk_lines(fs, r):
            try:
                obj = json.loads(line)
                ts = obj.get("ts") or obj.get("timestamp")
                if isinstance(ts, (int, float)):
                    if ts > 1e12:
                        ts /= 1000.0
                    if ts < args.t0 or ts > args.t1:
                        continue
            except Exception:
                pass
            sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent", help="Agent name (resolves home)")
    ap.add_argument("--home", help="Agent home path")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Create history + AA hot tip layout")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("tail-grok", help="Ingest new grok updates.jsonl lines into AA hot.jsonl")
    p.add_argument("--source", help="Path to updates.jsonl (default: live largest)")
    p.add_argument("--session-id", dest="session_id")
    p.add_argument("--max-lines", type=int, default=None)
    p.add_argument("--max-mib", type=float, default=None, help="Cap source bytes read this run")
    p.set_defaults(func=cmd_tail_grok)

    p = sub.add_parser("seal", help="Seal byte range (default source: AA hot.jsonl)")
    p.add_argument("--source", help="Default: history/hot.jsonl")
    p.add_argument("--session-id", dest="session_id")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=None)
    p.add_argument("--mib", type=float)
    p.add_argument("--level", type=int, default=3)
    p.add_argument("--label", default="seal")
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser("prune-hot", help="Prune AA hot.jsonl only (never backend-native)")
    p.add_argument("--source", help="Default: history/hot.jsonl")
    p.add_argument("--session-id", dest="session_id")
    p.add_argument("--max-mib", type=float, default=HOT_MAX_BYTES / 1024**2)
    p.add_argument("--keep-mib", type=float, default=HOT_KEEP_BYTES / 1024**2)
    p.add_argument("--level", type=int, default=3)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_prune_hot)

    p = sub.add_parser("list", help="List hot meta + manifest chunks")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("verify", help="Verify sealed chunks")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("cat-range", help="Emit archived JSONL for ts range")
    p.add_argument("--t0", type=float, required=True)
    p.add_argument("--t1", type=float, required=True)
    p.set_defaults(func=cmd_cat_range)

    args = ap.parse_args(argv)
    if not args.agent and not args.home:
        ap.error("need --agent or --home")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
