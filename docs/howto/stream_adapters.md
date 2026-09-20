# Stream adapters (backend → aa.stream)

## Backend ownership (locked)

Acquisition + normalization are **owned by AA backends**:

- `GrokBackend.configure_aa_history` + `sync_hot_stream` → `stream_adapters.grok.tail_grok_once`
- `ClaudeBackend` (same pattern when wired) → `stream_adapters.claude.tail_claude_once`
- `aa_stream.append_hot_events` — common writer only
- `full_stream_hook` — optional sidecar if `history/config.json` `owner=hook`

# Stream adapters (backend → aa.stream)

## Partition
```text
grok binary updates.jsonl
    → stream_adapters.grok (map/wrap/tail_once)
        → aa_stream.append_hot_events → history/hot.jsonl

claude session jsonl
    → stream_adapters.claude (stub)
        → same append_hot_events
```

| Module | Owns |
|--------|------|
| `stream_adapters.grok` | find updates, map_grok_event, wrap, tail_grok_once |
| `stream_adapters.claude` | stub for Astro |
| `aa_stream` | layout, build_event, append_hot_events, seal/prune helpers |
| `full_stream_hook` | inotify/poll orchestration, config tail_grok |

Compat: `aa_stream.tail_grok_once` still works (shim).
