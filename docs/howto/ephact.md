# Ephemeral Artifacts (ephact)

Ephemeral artifacts let agents pin focused content — tables, lists, code blocks — so they stay visible in the TUI while conversation scrolls beneath them. Think of it as a "frozen window" for content that would otherwise scroll away.

---

## How to Use

Wrap content in `<ephact>` tags in your normal speech:

```
<ephact type="table" title="Test Results">
| Test | Status |
|------|--------|
| boot | PASS |
| turn | FAIL |
</ephact>
```

That's it. The TUI detects the tags, extracts the content into a pinned viewer panel, and also renders the content inline in chat as a blockquote.

### Tag Format

```
<ephact type="TYPE" title="OPTIONAL TITLE">
content here
</ephact>
```

**Required attribute:**
- `type` — one of: `table`, `list`, `code`, `paragraph`

**Optional attribute:**
- `title` — display title for the viewer panel. If omitted, the type name is capitalized and used as the title.

### Content

Content between the tags is rendered as Rich Markdown in the viewer panel. Use standard markdown:
- Tables with `|` pipe syntax
- Bullet lists with `-` or `*`
- Code with triple backticks
- Regular paragraphs

### Important Notes

- **Tags inside code blocks are ignored.** If you put `<ephact>` inside backtick-fenced code (``` or inline `), the parser skips it. This means you can discuss ephact tags in code examples without triggering the viewer.
- **Streaming-safe.** Partial/unclosed tags are left in the text until the closing `</ephact>` arrives. No flicker or premature extraction.
- **Content stays in chat too.** The TUI does NOT strip tags from chat — it collects them AND renders them as blockquotes with a 📌 header in the conversation history. The viewer is an additional persistent display, not a replacement.
- **One tag at a time is fine, multiple tags in one message also work.** Each tag becomes a separate entry in the viewer stack.

---

## What the TUI Does

- **Viewer panel** appears between the chat scroll area and the input bar
- **Per-agent stacks** — each agent has its own artifact history
- **Tab bar** — bottom border shows labeled tabs for each ephact. Active tab is highlighted. Click any tab to switch directly.
- **Navigation:** F5 = previous, F6 = next in stack. ◀/▶ buttons in tab bar also navigate. All navigation wraps (going past the last item returns to the first, and vice versa).
- **Individual close** — each tab has a ✕ button to remove that ephact from the stack. If it was the last one, the viewer hides.
- **Toggle:** F3 shows/hides the entire viewer
- **Auto-height** — panel grows to fit content, max 50% of screen. Scrollbar appears if content exceeds the limit.
- **Tab switching** syncs the viewer to the active agent's stack
- **Scrollable tab bar** — when there are more tabs than fit in the panel width, ◀/▶ scroll the tab window. Active tab stays visible.
- **Copy mode** — F7 disables mouse capture for full native terminal text selection. Press F7 or Escape to exit.

### Archive

Each detected ephact is saved to `~/agents/<Name>/ephacts/ephact_<timestamp_ms>.json` with:
```json
{
  "type": "table",
  "title": "Test Results",
  "content": "| Test | Status |\n|------|--------|\n| boot | PASS |",
  "agent": "Trip",
  "timestamp": 1752768000.123
}
```

Agents can review their own past ephacts across sessions and compactions by reading files in their `ephacts/` directory.

---

## When to Use Ephacts

**Good uses:**
- Status tables, checklists, backlogs that need to stay visible during discussion
- Code snippets being iterated on
- Configuration blocks or command references
- Any content Eric will want to refer back to while continuing the conversation

**Don't use for:**
- Entire documents or very long content (viewer maxes at 50% screen)
- Content that's only relevant for one message (just say it normally)
- Decorative or filler content

---

## Examples

### Status table
```
<ephact type="table" title="Deploy Status">
| Service | Version | Status |
|---------|---------|--------|
| api     | 2.4.1   | ✓ live |
| worker  | 2.4.0   | rolling |
| web     | 2.4.1   | ✓ live |
</ephact>
```

### Checklist
```
<ephact type="list" title="Remaining Items">
- [ ] Write migration script
- [x] Update config schema
- [ ] Run integration tests
- [ ] Deploy to staging
</ephact>
```

### Code reference
```
<ephact type="code" title="Config Format">
[agent]
name = "Trip"
home = "/home/eric/agents/Trip"
model = "sxs-claude-opus-4-6"
</ephact>
```
