#!/usr/bin/env python3
"""memory_service MCP — embodiment memory control plane + full query.

Primary tool: memory_query (pack → workers → peer → judge → finish).
Workers spawn via headless `grok` (see scripts/spawn_grok_worker.py).

  PYTHONPATH=src MEMORY_SERVICE_ROOT=$PWD python3 -m memory_service_mcp.server
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]  # memory/
_ABIDE = Path(os.environ.get("AGENT_ABIDE_ROOT", str(_ROOT.parent)))  # agent-abide/
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ABIDE) not in sys.path:
    sys.path.insert(0, str(_ABIDE))
if str(_ABIDE / "scripts") not in sys.path:
    sys.path.insert(0, str(_ABIDE / "scripts"))

from mcp.server.fastmcp import FastMCP

ROOT = Path(os.environ.get("MEMORY_SERVICE_ROOT", str(_ROOT))).resolve()
SCRIPTS = ROOT / "scripts"
DEFAULT_AGENT = os.environ.get("MEMORY_SERVICE_AGENT", "Trip-G")
INLINE_BUDGET = int(os.environ.get("MEMORY_SERVICE_INLINE_BUDGET", "20000"))
WORKER_TIMEOUT = int(os.environ.get("MEMORY_WORKER_TIMEOUT", "2400"))


def resolve_agent_from_cwd(cwd: Path, agents: dict) -> dict | None:
    """Map a grok cwd onto agents.json by longest home prefix.

    Nested homes (``~/agents/LeviSmith/Squiggy``) must not resolve as the
    parent directory name (``LeviSmith``), which is not an agent and has
    no speech/conversation — empty memory packs.
    """
    try:
        cwd = Path(cwd).expanduser().resolve()
    except Exception:
        return None
    best = None  # (len(home), name, home)
    for name, a in (agents or {}).items():
        if not isinstance(a, dict):
            continue
        raw = a.get("home") or str(Path.home() / "agents" / name)
        try:
            home = Path(raw).expanduser().resolve()
        except Exception:
            continue
        try:
            cwd.relative_to(home)
        except ValueError:
            continue
        n = len(str(home))
        if best is None or n > best[0]:
            best = (n, str(name), str(home))
    if not best:
        return None
    return {"agent": best[1], "home": best[2]}


def _caller_agent_from_ppid() -> dict:
    """Best-effort: stdio parent grok cwd → agents.json name. HTTP: weak/empty."""
    out = {"ppid": None, "cwd": None, "agent": None, "home": None, "method": None}
    try:
        ppid = os.getppid()
        out["ppid"] = ppid
        cwd = Path(f"/proc/{ppid}/cwd").resolve()
        out["cwd"] = str(cwd)
        cfg = Path.home() / "agents/config/agents.json"
        agents = {}
        if cfg.exists():
            agents = json.loads(cfg.read_text()).get("agents") or {}
        hit = resolve_agent_from_cwd(cwd, agents)
        if hit:
            out.update(hit)
            out["method"] = "ppid_cwd_home_prefix"
            return out
        # fallback: first path component after …/agents/<Name>
        parts = cwd.parts
        if "agents" in parts:
            i = parts.index("agents")
            if i + 1 < len(parts):
                name = parts[i + 1]
                if name in agents:
                    out["agent"] = name
                    out["home"] = str(agents[name].get("home") or Path.home() / "agents" / name)
                    out["method"] = "ppid_cwd_agents_json"
                    return out
                if (Path.home() / "agents" / name).is_dir():
                    out["agent"] = name
                    out["home"] = str(Path.home() / "agents" / name)
                    out["method"] = "ppid_cwd_dirname"
    except Exception as e:
        out["error"] = str(e)
    return out



_HOST = os.environ.get("MEMORY_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("MEMORY_MCP_PORT", "8765"))

_INSTRUCTIONS = """
Look up what was said in your conversation record.
A memory worker whole-reads a packed form of that history and answers
as an embodiment of it.

Simplest call:
  memory_query(question="what's the difference between SA and thiasai?")

Default strategy is simple: one worker, your user-turn pack.
For heavy work (architecture from the record, high-stakes locks), use
strategy=n_runs (several workers + peer cross-check).

recall (replay tape, not judgment): memory_recall(query="any time eric talked about …")
Call memory_help once if you need more.
"""





mcp = FastMCP(
    "memory_service",
    instructions=_INSTRUCTIONS.strip(),
    host=_HOST,
    port=_PORT,
)



def _run_json(args: list[str], timeout: int = 120) -> dict:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if not out:
        return {"error": "empty_stdout", "stderr": err[:2000], "code": r.returncode, "cmd": args}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        start = out.find("{")
        end = out.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(out[start : end + 1])
                if r.returncode != 0:
                    data.setdefault("_code", r.returncode)
                    data.setdefault("_stderr", err[:1500])
                return data
            except json.JSONDecodeError:
                pass
        return {"error": "json_parse", "stdout": out[:4000], "stderr": err[:2000], "code": r.returncode}


def _clip(obj: dict | list | str, budget: int = INLINE_BUDGET) -> dict:
    raw = obj if isinstance(obj, str) else json.dumps(obj, indent=2)
    b = len(raw.encode()) if isinstance(raw, str) else len(json.dumps(obj).encode())
    text = raw if isinstance(raw, str) else json.dumps(obj, indent=2)
    b = len(text.encode())
    if b <= budget:
        return {"mode": "inline", "bytes": b, "tok_est": b // 4, "content": obj if not isinstance(obj, str) else obj}
    overflow = ROOT / "state" / "mcp_overflow"
    overflow.mkdir(parents=True, exist_ok=True)
    import hashlib, time
    h = hashlib.sha1(text.encode()).hexdigest()[:12]
    path = overflow / f"{int(time.time())}_{h}.json"
    path.write_text(text)
    return {
        "mode": "handle",
        "bytes": b,
        "tok_est": b // 4,
        "path": str(path),
        "preview": text[:1200],
        "note": "over MCP inline budget; full payload on disk",
    }


def _resolve_pack(agent: str, shape: str, run_dir: str | None = None) -> Path | None:
    shape = shape.upper()
    if run_dir:
        rd = Path(run_dir)
        if not rd.is_absolute():
            rd = ROOT / rd
        for cand in [
            rd / "corpus" / f"{shape}_users_only.txt",
            rd / "corpus" / f"{shape}_users_and_agents.txt",
            rd / "corpus" / f"{shape}_stratum.txt",
            rd / "corpus" / f"{shape}.txt",
        ]:
            if cand.exists():
                return cand
    runs = ROOT / "runs"
    if runs.exists():
        cands = sorted(runs.glob(f"*_{agent}"), key=lambda p: p.stat().st_mtime, reverse=True)
        for rd in cands:
            for name in (f"{shape}_users_only.txt", f"{shape}_users_and_agents.txt", f"{shape}.txt"):
                p = rd / "corpus" / name
                if p.exists():
                    return p
    if shape == "S03":
        p = ROOT / "experiments/s03_word_dist/S03_users_and_agents.txt"
        if p.exists():
            return p
    return None


# ── Primary: full embodiment query ─────────────────────────────────

@mcp.tool()
def memory_help() -> dict:
    """Short guide: how to ask your conversation record. Read once if unfamiliar."""
    return _clip({
        "what": (
            "Look up what was said in your conversation record. "
            "A worker whole-reads a packed form of that history and answers as an embodiment of it."
        ),
        "simplest": 'memory_query(question="what\'s the difference between SA and thiasai?")',
        "recall": 'memory_recall(query="any time eric talked about hardlinks") — replay, not synthesis',
        "defaults": {
            "whose_record": "yours (this grok session / agent home)",
            "what_is_packed": "user turns only (full dialogue with agent turns is too large)",
            "strategy": "simple",
            "n": 1,
            "peer": False,
        },
        "strategy_options": {
            "simple": (
                "DEFAULT. One worker reads your user-turn pack and answers. "
                "Fast/cheap. Good for ordinary questions."
            ),
            "n_runs": (
                "Several workers each read the same full user-turn pack, peer cross-check, "
                "then synthesize. Use for heavy work (e.g. define architecture from the record)."
            ),
            "turn_strata": (
                "Split the user-turn pack into time slices; each worker one slice. "
                "Only when the pack is too large for one worker."
            ),
        },
        "after_query": (
            "Response includes run_dir and handles. Read SYNTHESIS.md at the given path "
            "for the answer. memory_status / memory_result can poll that run_dir."
        ),
        "patterns": {
            "query": "memory_query — embody & answer (judgment)",
            "recall": "memory_recall — collect & replay (tape)",
        },
        "optional_args": {
            "strategy": "simple (default) | n_runs | turn_strata",
            "n": "default 1 (simple). n_runs/turn_strata: pass 3+ or rely on upgrade from 1→3",
            "peer": "default false (simple). n_runs: upgrades to true if left false",
        },
        "note": (
            "To ask about another agent\'s record, email/message that agent and have them "
            "run memory_query — do not pass another agent id here (for now)."
        ),
    })




@mcp.tool()
def memory_recall(
    query: str,
    window: int = 0,
    max_hits: int = 50,
    role: str | None = None,
) -> dict:
    """RECALL: collect & replay moments from your conversation (not a synthesis).

    Use when you want the tape — "any time Eric talked about X" — chronological
    excerpts in REPLAY.md. Does NOT judge or summarize doctrine.

    For "what did we decide / what's locked?" use memory_query instead.

    Args:
      query: What to find (natural language; stub uses keyword AND match).
      window: Extra speech lines before/after each hit (default 0).
      max_hits: Cap matching rows (default 50).
      role: Optional filter "user" or "assistant".
    """
    caller = _caller_agent_from_ppid()
    agent = caller.get("agent") or DEFAULT_AGENT
    cmd = [
        sys.executable, str(SCRIPTS / "recall.py"),
        "--agent", agent,
        "--query", query,
        "--window", str(window),
        "--max-hits", str(max_hits),
    ]
    if role:
        cmd.extend(["--role", role])
    return _clip(_run_json(cmd, timeout=120))



@mcp.tool()
def memory_query(
    question: str,
    strategy: str = "simple",
    n: int = 1,
    peer: bool = False,
    prepare_only: bool = False,
    timeout_sec: int = 0,
    base_run: str | None = None,
) -> dict:
    """Ask a question of YOUR conversation record.

    Packs your user turns, runs memory worker(s), then returns SYNTHESIS.md handle.

    Args:
      question: What to ask of the record.
      strategy:
        "simple" (default) — one worker, no peer. Ordinary questions.
        "n_runs" — several workers + peer. Heavy work (architecture from the record).
        "turn_strata" — split huge packs across workers.
      n: Worker count. Default 1 (matches simple). For n_runs/turn_strata use 3+ (or omit and
          n_runs upgrades bare defaults to n=3, peer=true).
      peer: Peer cross-check. Default false (matches simple). n_runs upgrades bare default to true.
      prepare_only: Scaffold only; no model workers (advanced).
      timeout_sec: Per-phase worker timeout (0 → 2400s).
      base_run: Reuse an existing pack run_dir (advanced).

    Whose record: calling agent only. To query another agent, message them to run this.
    """
    if strategy not in ("simple", "n_runs", "turn_strata"):
        return {"error": "strategy must be simple | n_runs | turn_strata"}
    # Schema defaults match simple (n=1, peer=false). Heavy strategies upgrade bare defaults.
    eff_strategy = "n_runs" if strategy == "simple" else strategy
    if strategy == "simple":
        n = 1
        peer = False
    elif strategy == "n_runs":
        # Schema defaults are simple-shaped (n=1, peer=false). A bare
        # strategy=n_runs with those leftovers upgrades to heavy defaults.
        # Explicit n=5 peer=false is respected.
        if n == 1 and peer is False:
            n, peer = 3, True
        elif n <= 1:
            n = 3
    elif strategy == "turn_strata":
        if n <= 1:
            n = 3
    caller = _caller_agent_from_ppid()
    agent = caller.get("agent") or DEFAULT_AGENT
    to = timeout_sec or WORKER_TIMEOUT
    cmd = [
        sys.executable, str(SCRIPTS / "memory_query.py"),
        "--agent", agent,
        "--question", question,
        "--strategy", eff_strategy,
        "--n", str(n),
        "--shapes", "S01",
        "--allow-expand", "S03",
        "--timeout-sec", str(to),
    ]
    if peer:
        cmd.append("--peer")
    else:
        cmd.append("--no-peer")
    if prepare_only:
        cmd.append("--prepare-only")
    if base_run:
        br = base_run if Path(base_run).is_absolute() else str(ROOT / base_run)
        cmd.extend(["--base-run", br])
    # full pipeline can take N * timeout * phases
    wall = to * (3 if peer else 2) + 600 if not prepare_only else 300
    return _clip(_run_json(cmd, timeout=wall))


@mcp.tool()
def run_workers(
    run_dir: str,
    phase: str = "a",
    timeout_sec: int = 0,
    max_parallel: int = 4,
) -> dict:
    """Spawn grok workers for pipeline phase a (workers), b (peers), or c (judge)."""
    rd = run_dir if Path(run_dir).is_absolute() else str(ROOT / run_dir)
    phase = phase.lower().replace("phase_", "")
    if phase not in ("a", "b", "c"):
        return {"error": "phase must be a|b|c"}
    to = timeout_sec or WORKER_TIMEOUT
    return _clip(_run_json([
        sys.executable, str(SCRIPTS / "run_phase_workers.py"),
        rd, "--phase", phase,
        "--cwd", str(ROOT),
        "--timeout-sec", str(to),
        "--max-parallel", str(max_parallel),
    ], timeout=to + 180))


@mcp.tool()
def memory_ask(
    question: str,
    sample: bool = False,
    n: int = 1,
    strategy: str = "simple",
) -> dict:
    """Pack your user-turn history + PROMPT only (no model workers). Advanced."""
    caller = _caller_agent_from_ppid()
    agent = caller.get("agent") or DEFAULT_AGENT
    cmd = [
        sys.executable, str(SCRIPTS / "memory_ask.py"),
        "--agent", agent,
        "--question", question,
        "--shapes", "S01",
        "--allow-expand", "S03",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    runs = sorted((ROOT / "runs").glob(f"*_{agent}"), key=lambda p: p.stat().st_mtime, reverse=True)
    base = None
    for c in runs:
        if "sample" not in c.name:
            base = c
            break
    if base is None and runs:
        base = runs[0]
    result = {
        "status": "ready_for_worker" if r.returncode == 0 and base else "error",
        "run_dir": str(base) if base else None,
        "question": question,
        "agent": agent,
        "code": r.returncode,
        "stderr_tail": (r.stderr or "")[-1000:],
    }
    if sample and base and r.returncode == 0:
        init = _run_json([
            sys.executable, str(SCRIPTS / "pipeline.py"), "init",
            "--base-run", str(base),
            "--n", str(n),
            "--strategy", strategy,
            "--question", question,
        ], timeout=180)
        result["sample"] = init
        result["sample_run_dir"] = init.get("run_dir")
    return _clip(result)


@mcp.tool()
def memory_status(run_dir: str) -> dict:
    """status.json + worker flags + pipeline status."""
    rd = Path(run_dir) if Path(run_dir).is_absolute() else ROOT / run_dir
    pipe = _run_json([sys.executable, str(SCRIPTS / "pipeline.py"), "status", str(rd)], timeout=60)
    return _clip(pipe)


@mcp.tool()
def memory_result(run_dir: str, which: str = "auto") -> dict:
    """Result handles: auto|synthesis|answer|stats. Prefer SYNTHESIS after sample."""
    rd = Path(run_dir) if Path(run_dir).is_absolute() else ROOT / run_dir
    prefer = {
        "auto": ["SYNTHESIS.md", "STATS.json", "ANSWER.md"],
        "synthesis": ["SYNTHESIS.md"],
        "answer": ["ANSWER_v2.md", "ANSWER.md"],
        "stats": ["STATS.json", "claims_judged.json"],
    }.get(which, ["SYNTHESIS.md"])
    found = []
    for name in prefer:
        p = rd / name
        if p.exists():
            b = p.stat().st_size
            rec: dict = {"name": name, "path": str(p), "bytes": b, "tok_est": b // 4, "mode": "handle"}
            if b <= INLINE_BUDGET:
                rec["mode"] = "inline"
                rec["content"] = p.read_text(encoding="utf-8", errors="replace")
            found.append(rec)
    wroot = rd / "workers"
    if wroot.exists() and which in ("auto", "answer"):
        for w in sorted(wroot.iterdir()):
            if not w.is_dir():
                continue
            for name in ("ANSWER_v2.md", "ANSWER.md", "PEER_REVIEW.md"):
                p = w / name
                if p.exists():
                    found.append({
                        "name": f"{w.name}/{name}",
                        "path": str(p),
                        "bytes": p.stat().st_size,
                        "mode": "handle",
                    })
    if not found:
        return {"error": "no_result_artifacts", "run_dir": str(rd)}
    return _clip({"run_dir": str(rd), "results": found})


@mcp.tool()
def describe_shape(
    shape: str = "S01",
    agent: str = DEFAULT_AGENT,
    run_dir: str | None = None,
    pack_path: str | None = None,
) -> dict:
    """Describe pack bytes/tok/n_turns."""
    pack = Path(pack_path) if pack_path else _resolve_pack(agent, shape, run_dir)
    if not pack or not pack.exists():
        return {"error": "pack_not_found", "shape": shape}
    return _clip(_run_json([sys.executable, str(SCRIPTS / "describe_shape.py"), str(pack)]))


@mcp.tool()
def get_shape(
    shape: str = "S01",
    agent: str = DEFAULT_AGENT,
    run_dir: str | None = None,
    pack_path: str | None = None,
) -> dict:
    """Shape pack handle + describe meta."""
    pack = Path(pack_path) if pack_path else _resolve_pack(agent, shape, run_dir)
    if not pack or not pack.exists():
        return {"error": "pack_not_found", "shape": shape}
    idx = pack.parent / "S01_users_index.json"
    cmd = [sys.executable, str(SCRIPTS / "get_shape.py"), str(pack), "--shape", shape.upper()]
    if idx.exists():
        cmd.extend(["--index", str(idx)])
    return _clip(_run_json(cmd))


@mcp.tool()
def get_segment(
    pack_path: str,
    around_turn: int | None = None,
    before: int = 1,
    after: int = 2,
    grep: str | None = None,
    lines_start: int | None = None,
    lines_end: int | None = None,
    shape: str = "S01",
    index_path: str | None = None,
    out_path: str | None = None,
) -> dict:
    """Carve segment by around_turn i, grep, or lines. Handle JSON."""
    pack = Path(pack_path)
    if not pack.is_absolute():
        pack = ROOT / pack
    cmd = [sys.executable, str(SCRIPTS / "get_segment.py"), "--pack", str(pack), "--shape", shape]
    if index_path:
        cmd.extend(["--index", index_path])
    if out_path:
        cmd.extend(["--out", out_path])
    if around_turn is not None:
        cmd.extend(["--around-turn", str(around_turn), "--before", str(before), "--after", str(after)])
    elif grep is not None:
        cmd.extend(["--grep", grep, "--before", str(before), "--after", str(after)])
    elif lines_start is not None and lines_end is not None:
        cmd.extend(["--lines", str(lines_start), str(lines_end)])
    else:
        return {"error": "need around_turn or grep or lines_start+lines_end"}
    return _clip(_run_json(cmd, timeout=60))


@mcp.tool()
def pipeline_prepare(run_dir: str, phase: str = "a", tau: float = 0.6) -> dict:
    """Mechanical prep only: a=spawn prompts, b=peer prompts, c=judge prompt."""
    rd = run_dir if Path(run_dir).is_absolute() else str(ROOT / run_dir)
    phase = phase.lower().replace("phase_", "")
    if phase not in ("a", "b", "c"):
        return {"error": "phase must be a|b|c"}
    cmd = [sys.executable, str(SCRIPTS / "pipeline.py"), f"prepare-{phase}", rd]
    if phase == "c":
        cmd.extend(["--tau", str(tau)])
    return _clip(_run_json(cmd, timeout=120))




def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="memory_service MCP server")
    ap.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MEMORY_MCP_TRANSPORT", "stdio"),
    )
    ap.add_argument("--host", default=None, help="override MEMORY_MCP_HOST")
    ap.add_argument("--port", type=int, default=None, help="override MEMORY_MCP_PORT")
    args = ap.parse_args()
    if args.host:
        mcp.settings.host = args.host
    if args.port is not None:
        mcp.settings.port = args.port
    # force localhost-only bind
    if mcp.settings.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing non-local bind host={mcp.settings.host!r}")
    print(
        f"memory_service MCP transport={args.transport} "
        f"http://{mcp.settings.host}:{mcp.settings.port}{mcp.settings.streamable_http_path}",
        flush=True,
    )
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
