#!/usr/bin/env python3
"""CLI: token-efficient transcript + user-index + expand from conversation.jsonl."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from v1_transcript import (  # noqa: E402
    TranscriptOptions,
    build_user_index,
    expand_around,
    format_user_index,
    generate_from_agent,
    generate_transcript,
    load_kept_entries,
    resolve_conversation_path,
    stats_summary,
    user_search,
)


def _opt_from_args(args) -> TranscriptOptions:
    if getattr(args, "memory_pack", False) or getattr(args, "cmd", None) in (
        "users", "search", "expand", "index",
    ):
        # memory-oriented defaults for index/expand
        opt = TranscriptOptions.memory_pack()
    else:
        opt = TranscriptOptions()
    if getattr(args, "include_thinking", False):
        opt.include_thinking = True
    if getattr(args, "include_system", False):
        opt.include_system = True
    if getattr(args, "keep_ops_user", False):
        opt.drop_ops_user = False
    if getattr(args, "no_merge", False):
        opt.merge_consecutive = False
    if getattr(args, "strip_attribution", False):
        opt.strip_attribution = True
    if getattr(args, "role_style", None) is not None:
        opt.role_style = args.role_style
    if getattr(args, "ts_style", None) is not None:
        opt.ts_style = args.ts_style
    if getattr(args, "md_headings", None) is not None:
        opt.md_headings = args.md_headings
    if getattr(args, "max_entry_chars", None):
        opt.max_entry_chars = args.max_entry_chars
    return opt


def _source_args(ap_or_p):
    ap_or_p.add_argument("--agent", "-a", help="Agent name under ~/agents")
    ap_or_p.add_argument("path", nargs="?", help="conversation.jsonl path")


def _resolve(args) -> Path:
    return resolve_conversation_path(args.agent, args.path)


def cmd_render(args) -> int:
    opt = _opt_from_args(args)
    if args.agent:
        result = generate_from_agent(args.agent, opt)
    elif args.path == "-" or (args.path is None and not sys.stdin.isatty()):
        result = generate_transcript(sys.stdin, opt)
    elif args.path:
        result = generate_transcript(Path(args.path), opt)
    else:
        print("need --agent, path, or stdin", file=sys.stderr)
        return 2
    _emit(result, args)
    return 0


def cmd_users(args) -> int:
    opt = _opt_from_args(args)
    path = _resolve(args)
    entries, stats = load_kept_entries(path, opt, merge=False)
    index = build_user_index(entries)
    full = not getattr(args, "snippets", False)
    show_meta = getattr(args, "meta", False)
    day_headers = not getattr(args, "no_day_headers", False)
    text = format_user_index(
        index, full_content=full, show_meta=show_meta, day_headers=day_headers
    )
    stats.chars_out = len(text)
    stats.kept_entries = len(index)
    if args.stats:
        print(
            f"users={len(index)} entries={len(entries)} "
            f"chars_out={len(text)} (~{len(text)//4} tok) "
            f"body={'full' if full else 'snippet'} "
            f"source_lines={stats.source_lines}",
            file=sys.stderr,
        )
    if args.json:
        if full:
            print(json.dumps(index, indent=2))
        else:
            slim = [{k: v for k, v in row.items() if k != "content"} for row in index]
            print(json.dumps(slim, indent=2))
    else:
        _write_out(text, args)
    if args.index_out:
        args.index_out.parent.mkdir(parents=True, exist_ok=True)
        args.index_out.write_text(json.dumps(index, indent=2) + chr(10))
        print(f"wrote index {args.index_out} ({len(index)} users)", file=sys.stderr)
    return 0



def cmd_search(args) -> int:
    opt = _opt_from_args(args)
    path = _resolve(args)
    entries, stats = load_kept_entries(path, opt, merge=False)
    index = build_user_index(entries)
    hits = user_search(index, args.query, limit=args.limit)
    if args.stats:
        print(f"hits={len(hits)} / users={len(index)} query={args.query!r}", file=sys.stderr)
    if args.json:
        print(json.dumps(hits, indent=2))
    else:
        sys.stdout.write(format_user_index(hits, with_header=True, full_content=False, show_meta=False))
    return 0


def cmd_expand(args) -> int:
    opt = _opt_from_args(args)
    if args.include_thinking:
        opt.include_thinking = True
        # reload with thinking kept — need include_thinking in should_keep
    path = _resolve(args)
    # if thinking wanted, build opt with thinking
    load_opt = TranscriptOptions(**{**opt.__dict__, "include_thinking": args.include_thinking})
    entries, _stats = load_kept_entries(path, load_opt, merge=False)

    handle = args.handle
    user_index = args.index
    entry_index = args.entry
    ts = args.ts
    result = expand_around(
        entries,
        handle=handle,
        user_index=user_index,
        entry_index=entry_index,
        ts=ts,
        before=args.before,
        after=args.after,
        include_thinking=args.include_thinking,
        opt=opt,
    )
    if args.stats:
        print(stats_summary(result.stats), file=sys.stderr)
        print(f"expand window entries={len(result.entries)}", file=sys.stderr)
    _emit(result, args)
    return 0


def _emit(result, args) -> None:
    if getattr(args, "stats", False) and getattr(args, "cmd", "") not in ("users", "search"):
        # users/search print their own
        if args.cmd == "render" or args.cmd is None:
            print(stats_summary(result.stats), file=sys.stderr)
    if getattr(args, "stats_json", False):
        print(json.dumps(asdict(result.stats)), file=sys.stderr)
    _write_out(result.text, args)


def _write_out(text: str, args) -> None:
    out = getattr(args, "out", None)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out} ({len(text)} chars)", file=sys.stderr)
    else:
        sys.stdout.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="V1 transcript / user-index / expand-around-turn"
    )
    sub = ap.add_subparsers(dest="cmd")

    # default render (also backward-compatible flat flags via render)
    def add_common(p):
        p.add_argument("--agent", "-a")
        p.add_argument("path", nargs="?")
        p.add_argument("-o", "--out", type=Path)
        p.add_argument("--memory-pack", action="store_true")
        p.add_argument("--include-thinking", action="store_true")
        p.add_argument("--include-system", action="store_true")
        p.add_argument("--keep-ops-user", action="store_true")
        p.add_argument("--no-merge", action="store_true")
        p.add_argument("--strip-attribution", action="store_true")
        p.add_argument("--role-style", choices=("short", "long", "none"), default=None)
        p.add_argument("--ts", choices=("none", "time", "iso"), default=None, dest="ts_style")
        p.add_argument(
            "--md-headings",
            choices=("keep", "single_hash", "strip_markers", "strip_and_drop_closers"),
            default=None,
        )
        p.add_argument("--max-entry-chars", type=int, default=0)
        p.add_argument("--stats", action="store_true")
        p.add_argument("--stats-json", action="store_true")

    p_render = sub.add_parser("render", help="Full plain transcript (default)")
    add_common(p_render)
    p_render.set_defaults(func=cmd_render)

    p_users = sub.add_parser("users", help="User-only index (Eric turns + handles)")
    add_common(p_users)
    p_users.add_argument("--json", action="store_true", help="JSON array to stdout")
    p_users.add_argument(
        "--snippets",
        action="store_true",
        help="One-line truncated snippets only (default: FULL user message bodies)",
    )
    p_users.add_argument(
        "--index-out", type=Path, help="Write JSON index sidecar"
    )
    p_users.set_defaults(func=cmd_users, memory_pack=True)

    p_search = sub.add_parser("search", help="Search user turns (substring)")
    add_common(p_search)
    p_search.add_argument("query", help="Substring to find in user turns")
    p_search.add_argument("--limit", type=int, default=50)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search, memory_pack=True)

    p_exp = sub.add_parser("expand", help="Full interaction around a user turn")
    add_common(p_exp)
    g = p_exp.add_mutually_exclusive_group(required=True)
    g.add_argument("--handle", "-H", help="u12 | e34 | ts=1789… | bare user index")
    g.add_argument("--index", "-i", type=int, help="User-turn index (uN)")
    g.add_argument("--entry", "-e", type=int, help="Kept-entry index (eN)")
    g.add_argument("--at-ts", type=float, dest="ts", help="Unix timestamp (nearest user)")
    p_exp.add_argument("--before", type=int, default=1, help="User turns before anchor")
    p_exp.add_argument("--after", type=int, default=2, help="User turns after anchor")
    p_exp.set_defaults(func=cmd_expand, memory_pack=True)

    # Backward compatible: no subcommand → render with flat flags
    # If first arg looks like subcommand, parse normally; else inject render
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if not argv_list or argv_list[0] not in (
        "render", "users", "search", "expand", "-h", "--help"
    ):
        # legacy flat mode → render
        if argv_list and argv_list[0] in ("--users-only",):
            # map --users-only to users subcommand
            argv_list = ["users"] + [a for a in argv_list if a != "--users-only"]
        else:
            argv_list = ["render"] + argv_list

    args = ap.parse_args(argv_list)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
