#!/usr/bin/env python3
"""End-to-end memory query: pack → strata/n_runs → workers → peer → judge → finish.

  python3 scripts/memory_query.py \\
    --agent Trip-G \\
    --question "What is locked about thiasai file sharing?" \\
    --strategy n_runs --n 3 --peer

Spawns real grok workers unless --prepare-only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent


def sh(args: list[str], timeout: int = 600) -> dict:
    r = subprocess.run(args, capture_output=True, text=True, cwd=str(ROOT), timeout=timeout)
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out) if out.startswith("{") else {"stdout": out}
    except json.JSONDecodeError:
        # find last json object
        start = out.rfind("\n{")
        if start >= 0:
            try:
                data = json.loads(out[start + 1 :])
            except json.JSONDecodeError:
                data = {"stdout": out[-4000:]}
        else:
            data = {"stdout": out[-4000:]}
    data["_code"] = r.returncode
    if r.stderr:
        data["_stderr"] = r.stderr[-1500:]
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="memory_query end-to-end")
    ap.add_argument("--agent", default="Trip-G")
    ap.add_argument("--question", required=True)
    ap.add_argument("--shapes", default="S01")
    ap.add_argument("--allow-expand", default="S03")
    ap.add_argument("--strategy", choices=["simple", "n_runs", "turn_strata"], default="simple")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--peer", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--tau", type=float, default=0.6)
    ap.add_argument("--prepare-only", action="store_true", help="no grok spawn; return manifests")
    ap.add_argument("--timeout-sec", type=int, default=2400)
    ap.add_argument("--base-run", type=Path, default=None, help="reuse existing pack run")
    ap.add_argument("--skip-finish", action="store_true")
    args = ap.parse_args()

    # argparse defaults are simple-oriented (n=1, peer=False).
    # When user selects heavy strategies without n/peer flags, upgrade defaults.
    if args.strategy == "simple":
        args.strategy = "n_runs"
        args.n = 1
        args.peer = False
    elif args.strategy in ("n_runs", "turn_strata"):
        # If n/peer still at simple defaults, apply heavy defaults
        # Detect via explicit sys.argv
        import sys as _sys
        if "--n" not in _sys.argv and "-n" not in _sys.argv:
            args.n = 3
        if "--peer" not in _sys.argv and "--no-peer" not in _sys.argv:
            args.peer = True if args.strategy == "n_runs" else args.peer
            if args.strategy == "n_runs":
                args.peer = True

    t0 = time.time()
    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "question": args.question,
        "agent": args.agent,
        "strategy": args.strategy,
        "n": args.n,
        "peer": args.peer,
        "steps": [],
    }

    # 1. base pack run via memory_ask unless base-run given
    if args.base_run:
        base = args.base_run.resolve()
    else:
        cmd = [
            sys.executable, str(SCRIPTS / "memory_ask.py"),
            "--agent", args.agent,
            "--question", args.question,
            "--shapes", args.shapes,
        ]
        if args.allow_expand:
            cmd.extend(["--allow-expand", args.allow_expand])
        r = sh(cmd, timeout=300)
        report["steps"].append({"memory_ask": r})
        # find newest run for agent
        # Prefer agent-local runs, then package runs/
        from pathlib import Path as _P
        import json as _json
        _cfg = _P.home() / "agents/config/agents.json"
        _home = _P.home() / "agents" / args.agent
        if _cfg.exists():
            _ag = (_json.loads(_cfg.read_text()).get("agents") or {}).get(args.agent) or {}
            if _ag.get("home"):
                _home = _P(_ag["home"])
        _roots = [_home / "memory" / "runs", ROOT / "runs"]
        cands = []
        for _r in _roots:
            if _r.exists():
                cands.extend(_r.glob(f"*_{args.agent}"))
        runs = sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)
        base = None
        for cand in runs:
            if "sample" not in cand.name:
                base = cand
                break
        if base is None and runs:
            base = runs[0]
        if base is None:
            print(json.dumps({"error": "no_base_run", **report}, indent=2))
            return 2

    report["base_run"] = str(base)

    # 2. pipeline init sample
    init_cmd = [
        sys.executable, str(SCRIPTS / "pipeline.py"), "init",
        "--base-run", str(base),
        "--n", str(args.n),
        "--strategy", args.strategy,
        "--question", args.question,
        "--tau", str(args.tau),
    ]
    if not args.peer:
        init_cmd.append("--skip-peer")
    init = sh(init_cmd, timeout=180)
    report["steps"].append({"pipeline_init": {k: init.get(k) for k in ("run_dir", "strategy", "n", "_code")}})
    run_dir = init.get("run_dir")
    if not run_dir:
        print(json.dumps({"error": "init_failed", "init": init, **report}, indent=2))
        return 2
    rd = Path(run_dir)
    report["run_dir"] = str(rd)

    if args.prepare_only:
        report["status"] = "prepare_only"
        report["next"] = "run_phase_workers a|b|c or pipeline prepare-*"
        report["elapsed_sec"] = round(time.time() - t0, 1)
        print(json.dumps(report, indent=2))
        return 0

    # 3. Phase A workers
    a = sh([
        sys.executable, str(SCRIPTS / "run_phase_workers.py"),
        str(rd), "--phase", "a",
        "--cwd", str(ROOT),
        "--timeout-sec", str(args.timeout_sec),
    ], timeout=args.timeout_sec + 120)
    report["steps"].append({"phase_a": {"ok": a.get("ok"), "artifacts": a.get("artifacts"), "_code": a.get("_code")}})
    if not a.get("ok"):
        report["status"] = "failed_phase_a"
        report["phase_a_detail"] = a
        report["elapsed_sec"] = round(time.time() - t0, 1)
        print(json.dumps(report, indent=2))
        return 1

    # 4. Peer
    if args.peer:
        pb = sh([sys.executable, str(SCRIPTS / "pipeline.py"), "prepare-b", str(rd)], timeout=120)
        report["steps"].append({"prepare_b": pb.get("phase") or pb})
        b = sh([
            sys.executable, str(SCRIPTS / "run_phase_workers.py"),
            str(rd), "--phase", "b",
            "--cwd", str(ROOT),
            "--timeout-sec", str(args.timeout_sec),
        ], timeout=args.timeout_sec + 120)
        report["steps"].append({"phase_b": {"ok": b.get("ok"), "artifacts": b.get("artifacts"), "_code": b.get("_code")}})
        if not b.get("ok"):
            report["status"] = "failed_phase_b"
            report["elapsed_sec"] = round(time.time() - t0, 1)
            print(json.dumps(report, indent=2))
            return 1

    # 5. Judge only when peer (n_runs). simple n=1: mechanical merge.
    # prepare-c / JUDGE_PROMPT.md are for the stats-judge worker; skip-peer
    # runs never write that file, so phase c was failing no_JUDGE_PROMPT.
    if args.peer:
        pc = sh([
            sys.executable, str(SCRIPTS / "pipeline.py"), "prepare-c", str(rd),
            "--tau", str(args.tau),
        ], timeout=120)
        report["steps"].append({"prepare_c": "ok" if pc.get("_code") == 0 else pc})
        c = sh([
            sys.executable, str(SCRIPTS / "run_phase_workers.py"),
            str(rd), "--phase", "c",
            "--cwd", str(ROOT),
            "--timeout-sec", str(args.timeout_sec),
        ], timeout=args.timeout_sec + 120)
        report["steps"].append({"phase_c": {"ok": c.get("ok"), "artifacts": c.get("artifacts"), "_code": c.get("_code")}})
        if not c.get("ok"):
            report["status"] = "failed_phase_c"
            report["elapsed_sec"] = round(time.time() - t0, 1)
            print(json.dumps(report, indent=2))
            return 1
    else:
        merged = sh([
            sys.executable, str(SCRIPTS / "synthesize.py"), "merge", str(rd),
            "--tau", str(args.tau),
        ], timeout=180)
        report["steps"].append({"merge": {
            "n_claims": merged.get("n_claims"),
            "_code": merged.get("_code"),
        }})
        if merged.get("_code") not in (0, None) and "error" in merged:
            report["status"] = "failed_merge"
            report["elapsed_sec"] = round(time.time() - t0, 1)
            print(json.dumps(report, indent=2))
            return 1

    # 6. finish
    if not args.skip_finish:
        fin = sh([
            sys.executable, str(SCRIPTS / "finish_run.py"), str(rd),
            "--agent", args.agent,
        ], timeout=300)
        report["steps"].append({"finish": fin.get("edge_scores") or fin})

    # 7. result handles
    results = []
    for name in ("SYNTHESIS.md", "STATS.json", "claims_judged.json"):
        p = rd / name
        if p.exists():
            results.append({"name": name, "path": str(p), "bytes": p.stat().st_size, "mode": "handle"})
    report["results"] = results
    report["status"] = "complete"
    report["elapsed_sec"] = round(time.time() - t0, 1)
    (rd / "MEMORY_QUERY_REPORT.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
