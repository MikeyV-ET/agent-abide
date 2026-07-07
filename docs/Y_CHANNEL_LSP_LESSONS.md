# Y-Channel Design: Lessons from LSP

Research into Language Server Protocol architectural drawbacks and how they apply
to our Y-channel design (a nexus owning the grok binary stdio connection, routing
to both asdaaas and the observer).

**Origin:** Eric + Trip discussion, 2026-07-06. Research conducted 2026-07-07.
**Notebook reference:** Trip lab_notebook_trip.md, entry 2026-07-07 10:21 PDT.

---

## What is the Y-channel?

The grok binary communicates over two channels:
- **updates.jsonl / events.jsonl** — data stream (tool calls, text, events)
- **stdout JSON-RPC** — control channel (gates, permissions, skill catalogs)

The observer currently only watches updates.jsonl — it's half-blind. The Y-channel
concept places a nexus between the binary and asdaaas that sees the full stream,
giving the observer complete visibility.

```
Binary ←→ Y-channel ←→ asdaaas
                ↓
            observer
```

## LSP as structural analogue

LSP is the closest existing pattern: JSON-RPC over stdio, bidirectional,
server-to-client requests (gates). Widely deployed, well-documented failure modes.

---

## Critical Drawbacks

### 1. Pipe buffer deadlock

**The problem:** OS pipes have fixed kernel buffers (~64KB on Linux). When both
directions fill simultaneously — each side blocking on write, waiting for the
other to read — permanent deadlock. Neither side can make progress.

**How it happens in LSP:** Large payloads (diagnostics, completion lists, semantic
tokens) + rapid notifications (didChange on every keystroke) can fill buffers in
both directions simultaneously.

**Mitigation:** Async read loops that continuously drain input, independent of
writing. Non-blocking I/O. Dedicated reader tasks per direction. Never do
blocking work on the forwarding path.

**Applies to us:** The Y forwards stdin→binary and binary→stdout. If the tap
(logging, parsing) blocks the forwarding loop, or if gate response injection
blocks while binary is writing, we deadlock. Current prototype (_process_stdout
as async coroutine) is safe, but any blocking addition to the hot path is
dangerous.

**Rule: Never block the forwarding loop. All tap work must be non-blocking.**

### 2. Server-to-client request blocking

**The problem:** LSP has server-to-client requests (`window/showMessageRequest`,
`window/showDocument`) where the server blocks waiting for the client to respond.
In headless/CI environments with no human, these deadlock the session.

**Real-world examples:**
- ElixirLS: "Shall I install hex?" — CI hangs
- Metals (Scala): BSP connection prompt — headless hangs
- ansible-language-server: unhandled promise rejection when client has no handler
- tower-lsp: client request futures hang due to serialization bugs

**Mitigation:** Auto-respond with null/safe default in headless mode. Don't
advertise the capability. Use notifications instead of requests.

**Applies to us:** This IS our gate problem. The grok binary sends `_x.ai/exit_plan_mode`
and `_x.ai/ask_user_question` as server-to-client requests, blocking on stdout
until the client responds on stdin. Sr's gate handlers auto-respond. The Y-channel
must preserve this capability — it cannot be a passive tee.

**Rule: Y must be an active participant (proxy), not a passive observer (tee).**

---

## High-Priority Drawbacks

### 3. Message framing / partial reads

**The problem:** Large messages arrive fragmented across multiple read() calls.
Naive line-based or single-read() parsing desynchronizes the stream. The ls_proxy
project explicitly documented "large messages broken up causing parsing issues."

**LSP uses Content-Length framing.** Our binary uses newline-delimited JSON-RPC,
which is simpler — but large messages (skill catalog is 2-3K tokens) could still
span multiple read() chunks.

**Current code splits on `\n`, which handles this correctly.** Lower risk than LSP
but needs validation with real large payloads.

**Rule: Always validate complete messages before forwarding.**

### 4. Tracing / logging volume

**The problem:** Verbose LSP tracing is a known performance killer. Full
request/response logging causes CPU/IO-bound behavior. Emacs lsp-mode docs
explicitly warn: leaving `lsp-log-io` enabled "causes a large performance hit."

**Applies to us:** The stdout_log.jsonl tap logs every frame with flush() per
write. If the binary is chatty (available_commands_update, permission requests,
skill catalogs), the log grows fast.

**Rule: Full tap for debugging, filtered/sampled for production. Make it
configurable.**

---

## Moderate-Priority Drawbacks

### 5. Proxy architecture pitfalls

Lessons from LSP proxy implementations (ls_proxy, lsp-proxy, Garnix):

- **Concurrency:** Need separate async tasks for each direction + non-blocking
  channels for middleware/tee side effects.
- **Request ID tracking:** Needed if proxy issues its own requests or rewrites
  responses. Our gate auto-responses already need this.
- **Clean shutdown:** Child crash (EOF on stdout) must propagate cleanly. Zombie
  processes are a common LSP proxy bug.
- **Platform quirks:** Windows binary vs text mode, CRLF conversion breaks
  parsing. Relevant if we ever run cross-platform.

**Current recommendation:** Keep the Y embedded in grok_backend.py (same process).
Extract to standalone only if observer needs independent lifecycle. Embedded
avoids all process lifecycle complexity.

### 6. Process overhead

An additional proxy process adds memory, CPU, and another failure point. LSP
proxies are lightweight but add latency and management burden.

**For us:** Embedded = no extra process. Standalone = one more, but lightweight.

---

## Design Principles (Summary)

1. **Never block the forwarding loop.** All tap/middleware work must be
   non-blocking (async queue, fire-and-forget).
2. **Y is a proxy, not a tee.** Gate auto-response means the Y injects data,
   not just observes. Active participant.
3. **Validate complete messages before forwarding.** Don't forward partial data.
4. **Logging volume needs controls.** Full tap for debug, filtered for production.
5. **Keep embedded unless forced to extract.** Extra process = extra complexity
   without benefit unless observer needs independent lifecycle.
6. **Two concurrent loops minimum.** stdin→binary and binary→stdout must run
   independently (already the case).

---

## Sources

- GitHub: vim/vim#2548, ansible/vscode-ansible#1144, scalameta/metals#7941,
  JakeBecker/ide-elixir#12, axelson/ls_proxy, techee/lsp-proxy
- Blogs: garnix.io/blog/taking-lsp-one-step-further,
  emacs-lsp.github.io/lsp-mode/page/performance
- Academic: cs61.seas.harvard.edu/site/2018/Synch5 (pipe buffer deadlock)
- Spec: microsoft.github.io/language-server-protocol (3.17)
