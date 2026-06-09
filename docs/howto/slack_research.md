# Slack Research Adapter

On-demand Slack channel reading for agents. Control adapter -- you send commands, get results back as doorbells.

## Starting the Adapter

The adapter is not always running. Start it when you need it:

```bash
setsid nohup python3 /home/eric/projects/agent-abide/adapters/slack_research_adapter.py \
  --agents Cinco --poll-interval 1.0 > /tmp/slack_research_cinco.log 2>&1
```

Check if already running: `ps aux | grep slack_research | grep -v grep`

## Sending Commands

Commands go through the adapter outbox as **standard message envelopes**. The command JSON goes inside the `text` field, stringified:

```python
import json, os, time, secrets, uuid

outbox = os.path.expanduser('~/agents/<Name>/asdaaas/adapters/slack_research/outbox')
os.makedirs(outbox, exist_ok=True)

msg = {
    'id': str(uuid.uuid4()),
    'from': '<Name>',
    'to': 'slack_research',
    'text': json.dumps({'command': 'history', 'channel': '#stemm-action-bench', 'limit': 200}),
    'adapter': 'slack_research',
    'ts': time.time()
}

fname = f'cmd_{int(time.time()*1000)}_{secrets.token_hex(4)}.json'
with open(os.path.join(outbox, fname), 'w') as f:
    json.dump(msg, f)
```

**Common mistake:** Writing bare command JSON (`{"command": "channels"}`) instead of wrapping it in the envelope. The adapter parses the envelope's `text` field -- bare JSON silently fails with "Unknown command."

## Commands

### channels
List all channels the token has access to (max 200).
```json
{"command": "channels"}
```
Returns: `{status, channels: [{id, name, is_member, num_members, topic, purpose}]}`

### search
Search messages across all channels.
```json
{"command": "search", "query": "hackathon", "count": 20}
```
- `count`: max results (1-100, default 20)
- `sort`: `"timestamp"` (default) or `"score"`
- `sort_dir`: `"desc"` (default) or `"asc"`

Returns: `{status, total, returned, matches: [{channel, channel_id, user, text, ts, permalink}]}`

### history
Read channel messages in chronological order.
```json
{"command": "history", "channel": "#general", "limit": 50}
```
- `channel`: channel name (`#general`) or ID (`C060T7R72P9`)
- `limit`: max messages (1-200, default 50)
- `topic`: optional keyword filter (case-insensitive)
- `oldest`: timestamp for pagination (fetch messages after this point)

Returns: `{status, channel, channel_id, total, has_more, messages: [{user, text, ts, thread_ts, reply_count}]}`

**Pagination:** If `has_more` is true, take the last message's `ts` and pass it as `oldest` in the next request.

### thread
Read a thread's replies.
```json
{"command": "thread", "channel": "#general", "ts": "1774388146.705699"}
```
- `ts`: the parent message's timestamp (from history results)

Returns: `{status, channel, thread_ts, replies: [{user, text, ts}]}`

### status
Adapter health check.
```json
{"command": "status"}
```

## Reading Results

Results arrive in your adapter inbox as doorbells:
```
~/agents/<Name>/asdaaas/adapters/slack_research/inbox/msg_*.json
```

Parse the `text` field (JSON string) to get the result payload:
```python
import json
with open(inbox_file) as f:
    envelope = json.load(f)
result = json.loads(envelope['text'])
print(result['status'])  # "ok" or "error"
```

## Notes

- Uses Eric's user token (`xoxp-`) stored at `~/.mikeyv_creds/slack_token`. Token scope determines which channels are visible.
- Channel name resolution caches for 5 minutes. The cache is limited to 200 channels from `conversations.list`. If a channel doesn't resolve by name, use its ID directly (find it via `search`).
- User IDs in results are raw Slack IDs (e.g. `U0994JNQ9B6`), not display names. Use search or channel context to map them.
- The adapter polls your outbox every 1 second. Results typically arrive within 2-3 seconds.

## Research Methodology

When analyzing a Slack channel, go deep before filtering:

1. **Level 0 -- All messages.** Pull full `history` (limit 200, paginate if `has_more`). This is your index.
2. **Level 1 -- All threads.** Pull EVERY thread, not a subset. Reply count is a bad proxy for information density. A 1-reply thread can contain the most substantive information in the channel. Pulling 30 threads costs ~30 seconds of adapter time. Don't filter before reading.
3. **Level 2 -- Linked documents.** Note all URLs (Google Docs, GDrive, GitHub). Access what you can.
4. **Level 3 -- User identification.** Map user IDs to names from message context (mentions, signatures, display names in search results).

Don't prioritize threads by reply count. A logistics thread with 22 replies about GDrive permissions is less informative than a 1-reply thread where a lead explains system architecture.