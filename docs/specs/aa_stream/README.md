# AA Stream (V2 full stream / hot format)

**Lives in agent-abide** — this directory is the canonical home for format specs and version records. Agent notebooks may link here; they are not the source of truth.

| Doc | Purpose |
|-----|---------|
| [HOT_FORMAT_v1.md](./HOT_FORMAT_v1.md) | **`aa.stream` v1** — hot tip + chunk line format, version registry, meta/manifest |
| [HISTORY_VERSIONS.md](./HISTORY_VERSIONS.md) | V1–V5 roles, hot policy 300→100, L1–L4 ship ladder |
| [AUDIT_backend_session_schemas.md](./AUDIT_backend_session_schemas.md) | Grok / Claude / Codex on-disk research (design input) |

## Code (same repo)

| Path | Role |
|------|------|
| `core/full_stream.py` | L1 seal / prune / manifest |
| `scripts/v2_full_stream.py` | CLI |
| `tests/test_full_stream_l1.py` | L1 tests |
| `core/aa_stream.py` | aa.stream v1 envelope + grok tailer |
| `tests/test_aa_stream_hot.py` | hot + tailer + prune guard tests |

## Version record

Format family **`aa.stream`**, integer **`v`**. Registry table lives in `HOT_FORMAT_v1.md` §3. When bumping to v2, add `HOT_FORMAT_v2.md` and leave v1 immutable in-tree.

## Product intent

- AA-owned `history/hot.jsonl` (not backend-native prune)
- Normalized spine/body for clients + required `native` for fidelity
- **SA first** (L3); AA-TUI and backend cleanup (L4) later

## CLI (quick)

```bash
python3 scripts/v2_full_stream.py --agent Trip-G init
python3 scripts/v2_full_stream.py --agent Trip-G tail-grok          # ingest grok → hot.jsonl
python3 scripts/v2_full_stream.py --agent Trip-G prune-hot          # dry-run AA hot only
python3 scripts/v2_full_stream.py --agent Trip-G list
```

Prune/apply **refuses** `~/.grok/sessions`, `~/.codex/sessions`, `~/.claude/projects`.
