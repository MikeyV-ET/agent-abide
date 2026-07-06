#!/usr/bin/env python3
"""audit_session.py -- Audit updates.jsonl for agent intention vs asdaaas behavior.

Reads updates.jsonl within time boundaries and produces a report showing:
1. Agent intention: delay commands set, acks sent, gaze changes
2. Asdaaas behavior: continues fired, doorbells delivered, compaction events
3. Mismatches: triplicate continues, continues despite pending delays,
   continues before delay expiry, unacked doorbells

Usage:
    python3 audit_session.py --agent Trip
    python3 audit_session.py --agent Trip --last 2h
    python3 audit_session.py --agent Trip --from "2026-06-22 07:00" --to "2026-06-22 08:00"
    python3 audit_session.py --session-dir /path/to/session --last 1h
    python3 audit_session.py --agent Trip --last 4h --verbose
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PDT = timezone(timedelta(hours=-7))


def find_session_dir(agent_name: str, agents_home: str | None = None) -> Path:
    """Find the active (most recently modified) session directory for an agent."""
    home = agents_home or str(Path.home() / "agents")
    encoded = "%2F" + "%2F".join(f"{home}/{agent_name}".strip("/").split("/"))
    base = Path.home() / ".grok" / "sessions" / encoded
    if not base.exists():
        print(f"No sessions found for agent '{agent_name}' at {base}", file=sys.stderr)
        sys.exit(1)

    sessions = [d for d in base.iterdir() if d.is_dir()]
    if not sessions:
        print(f"No session directories in {base}", file=sys.stderr)
        sys.exit(1)

    # Pick the most recently modified session
    sessions.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return sessions[0]


def parse_duration(s: str) -> int:
    """Parse a duration string like '2h', '30m', '1d' into seconds."""
    m = re.match(r'^(\d+)([smhd])$', s)
    if not m:
        print(f"Invalid duration: '{s}'. Use format like 2h, 30m, 1d.", file=sys.stderr)
        sys.exit(1)
    val = int(m.group(1))
    unit = m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return val * multipliers[unit]


def parse_time(s: str) -> int:
    """Parse a time string into unix timestamp. Assumes PDT."""
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%H:%M', '%H:%M:%S'):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.year == 1900:  # time-only format
                now = datetime.now(tz=PDT)
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
            dt = dt.replace(tzinfo=PDT)
            return int(dt.timestamp())
        except ValueError:
            continue
    print(f"Cannot parse time: '{s}'", file=sys.stderr)
    sys.exit(1)


def ts_str(ts: int) -> str:
    """Format timestamp as HH:MM:SS PDT."""
    return datetime.fromtimestamp(ts, tz=PDT).strftime('%H:%M:%S')


def ts_full(ts: int) -> str:
    """Format timestamp as YYYY-MM-DD HH:MM:SS PDT."""
    return datetime.fromtimestamp(ts, tz=PDT).strftime('%Y-%m-%d %H:%M:%S')


def parse_updates(path: Path, start_ts: int, end_ts: int):
    """Parse updates.jsonl within time bounds, return categorized events."""
    events = {
        'delays': [],
        'acks': [],
        'continues': [],
        'doorbells': [],
        'compaction_events': [],
        'compaction_msgs': [],
        'user_msgs': [],
        'agent_turns': [],  # first substantial message chunk per turn
        'tool_calls': [],
        'doom_loops': [],
    }

    seen_turns = set()

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                ts = d.get('timestamp', 0)
                if ts < start_ts or ts > end_ts:
                    continue

                su = d.get('params', {}).get('update', {}).get('sessionUpdate', '')
                meta = d.get('params', {}).get('_meta', {})

                if su == 'user_message_chunk':
                    text = d['params']['update'].get('content', {}).get('text', '')
                    if '[continue' in text:
                        m = re.search(r'id=(cont_\w+)', text)
                        cid = m.group(1) if m else '?'
                        events['continues'].append({
                            'ts': ts, 'id': cid, 'text': text[:300]
                        })
                    elif 'Compaction complete' in text or 'Context reduced' in text:
                        events['compaction_msgs'].append({
                            'ts': ts, 'text': text[:300]
                        })
                    elif text.startswith('<eric') or text.startswith('[eric'):
                        events['user_msgs'].append({
                            'ts': ts, 'text': text[:300]
                        })
                    elif 'localmail' in text.lower() or 'remind' in text.lower() or 'bell_' in text:
                        events['doorbells'].append({
                            'ts': ts, 'text': text[:300]
                        })
                    elif text.startswith('[system') or text.startswith('[tui') or text.startswith('[irc'):
                        events['doorbells'].append({
                            'ts': ts, 'text': text[:300]
                        })

                elif su == 'tool_call':
                    raw = d['params']['update'].get('rawInput', {})
                    cmd = raw.get('command', '')
                    if cmd and 'asdaaas/commands' in cmd:
                        if 'delay' in cmd:
                            # Handle both JSON double-quote and Python single-quote formats
                            m_sec = re.search(r"""['"]seconds['"]\s*:\s*['"]?(until_event|\d+)['"]?""", cmd)
                            delay_val = m_sec.group(1) if m_sec else '?'
                            m_ack = re.search(r"""['"]ack['"]\s*:\s*\[([^\]]*)\]""", cmd)
                            ack_ids = m_ack.group(1).strip().strip("'\"") if m_ack else None
                            events['delays'].append({
                                'ts': ts, 'seconds': delay_val, 'ack': ack_ids
                            })
                            if ack_ids:
                                events['acks'].append({
                                    'ts': ts, 'ids': ack_ids
                                })
                        elif '"action": "ack"' in cmd or '"action":"ack"' in cmd:
                            m_ack = re.search(r'"handled":\s*\[([^\]]*)\]', cmd)
                            ack_ids = m_ack.group(1).strip() if m_ack else '?'
                            events['acks'].append({
                                'ts': ts, 'ids': ack_ids
                            })
                        elif 'gaze' in cmd:
                            events['tool_calls'].append({
                                'ts': ts, 'type': 'gaze', 'cmd': cmd[:200]
                            })
                        elif 'awareness' in cmd:
                            events['tool_calls'].append({
                                'ts': ts, 'type': 'awareness', 'cmd': cmd[:200]
                            })
                        elif 'compact' in cmd:
                            events['tool_calls'].append({
                                'ts': ts, 'type': 'compact_request', 'cmd': cmd[:200]
                            })

                elif su == 'agent_message_chunk':
                    text = d['params']['update'].get('content', {}).get('text', '')
                    turn_id = meta.get('promptId', '')
                    if turn_id and turn_id not in seen_turns and len(text) > 10:
                        seen_turns.add(turn_id)
                        events['agent_turns'].append({
                            'ts': ts, 'text': text[:200],
                            'tokens': meta.get('totalTokens', 0)
                        })

                elif su == 'auto_compact_completed':
                    upd = d['params']['update']
                    events['compaction_events'].append({
                        'ts': ts,
                        'tokens_before': upd.get('tokens_before', 0),
                        'tokens_after': upd.get('tokens_after', 0),
                    })

                elif su == 'doom_loop_detected':
                    events['doom_loops'].append({'ts': ts})

            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    return events


def analyze(events, verbose=False):
    """Produce audit findings from parsed events."""
    findings = []
    timeline = []

    # === TRIPLICATES ===
    # Group continues by 5-minute windows
    continues = events['continues']
    windows = defaultdict(list)
    for c in continues:
        window = c['ts'] // 300
        windows[window].append(c)
    for w, group in sorted(windows.items()):
        if len(group) >= 3:
            start = ts_str(group[0]['ts'])
            end = ts_str(group[-1]['ts'])
            span = group[-1]['ts'] - group[0]['ts']
            findings.append({
                'type': 'TRIPLICATE',
                'severity': 'HIGH',
                'ts': group[0]['ts'],
                'message': f"{len(group)} continues in {span}s ({start}-{end})",
                'details': [f"  {ts_str(c['ts'])} {c['id']}" for c in group],
            })

    # === CONTINUE DESPITE DELAY ===
    for c in continues:
        recent_delays = [d for d in events['delays'] if d['ts'] < c['ts']]
        if not recent_delays:
            continue
        last_delay = recent_delays[-1]
        gap = c['ts'] - last_delay['ts']
        delay_val = last_delay['seconds']

        # Check if any user message or doorbell came between delay and continue
        # (which would legitimately wake an until_event delay)
        intervening = [
            e for e in events['user_msgs'] + events['doorbells']
            if last_delay['ts'] < e['ts'] < c['ts']
        ]

        if delay_val == 'until_event':
            if gap < 30 and not intervening:
                findings.append({
                    'type': 'CONTINUE_DESPITE_UNTIL_EVENT',
                    'severity': 'MEDIUM',
                    'ts': c['ts'],
                    'message': f"Continue {c['id']} at {ts_str(c['ts'])}, {gap}s after until_event delay (no intervening event)",
                })
        elif delay_val.isdigit():
            expected = int(delay_val)
            if gap < expected * 0.9 and not intervening:
                findings.append({
                    'type': 'CONTINUE_BEFORE_DELAY_EXPIRED',
                    'severity': 'MEDIUM',
                    'ts': c['ts'],
                    'message': f"Continue {c['id']} at {ts_str(c['ts'])}, {gap}s into {expected}s delay",
                })

    # === COMPACTION STALE REPORT ===
    for msg in events['compaction_msgs']:
        m = re.search(r'from (\d+) to (\d+)', msg['text'])
        if m:
            before = int(m.group(1))
            after = int(m.group(2))
            if before == after:
                findings.append({
                    'type': 'STALE_COMPACTION_REPORT',
                    'severity': 'HIGH',
                    'ts': msg['ts'],
                    'message': f"Compaction report at {ts_str(msg['ts'])}: {before} == {after} (issue_0022 pattern)",
                })

    # === DOOM LOOP ===
    for dl in events['doom_loops']:
        findings.append({
            'type': 'DOOM_LOOP',
            'severity': 'CRITICAL',
            'ts': dl['ts'],
            'message': f"Doom loop detected at {ts_str(dl['ts'])}",
        })

    # Sort findings by timestamp
    findings.sort(key=lambda f: f['ts'])

    return findings


def build_timeline(events):
    """Build a chronological timeline of all events."""
    timeline = []
    for c in events['continues']:
        timeline.append((c['ts'], 'CONTINUE', c['id']))
    for d in events['delays']:
        ack_note = f" (ack: {d['ack']})" if d['ack'] else ""
        timeline.append((d['ts'], 'DELAY', f"{d['seconds']}{ack_note}"))
    for a in events['acks']:
        timeline.append((a['ts'], 'ACK', a['ids']))
    for u in events['user_msgs']:
        timeline.append((u['ts'], 'USER', u['text'][:100]))
    for db in events['doorbells']:
        timeline.append((db['ts'], 'DOORBELL', db['text'][:100]))
    for ce in events['compaction_events']:
        timeline.append((ce['ts'], 'COMPACT', f"{ce['tokens_before']} -> {ce['tokens_after']}"))
    for cm in events['compaction_msgs']:
        timeline.append((cm['ts'], 'COMPACT_MSG', cm['text'][:100]))
    for at in events['agent_turns']:
        timeline.append((at['ts'], 'AGENT', f"[{at['tokens']}tok] {at['text'][:80]}"))
    for tc in events['tool_calls']:
        timeline.append((tc['ts'], 'CMD', f"{tc['type']}: {tc.get('cmd', '')[:80]}"))
    for dl in events['doom_loops']:
        timeline.append((dl['ts'], 'DOOM', 'doom_loop_detected'))

    timeline.sort(key=lambda x: x[0])
    return timeline


def print_report(events, findings, timeline, verbose, start_ts, end_ts):
    """Print the audit report."""
    print("=" * 70)
    print("UPDATES.JSONL AUDIT REPORT")
    print(f"Time range: {ts_full(start_ts)} — {ts_full(end_ts)}")
    print("=" * 70)

    # Summary
    print(f"\n--- Event Counts ---")
    print(f"  Continues received:    {len(events['continues'])}")
    print(f"  Delays set:            {len(events['delays'])}")
    print(f"  Acks sent:             {len(events['acks'])}")
    print(f"  Doorbells received:    {len(events['doorbells'])}")
    print(f"  User messages:         {len(events['user_msgs'])}")
    print(f"  Agent turns:           {len(events['agent_turns'])}")
    print(f"  Compaction events:     {len(events['compaction_events'])}")
    print(f"  Compaction messages:   {len(events['compaction_msgs'])}")
    print(f"  Doom loops:            {len(events['doom_loops'])}")

    # Findings
    if findings:
        print(f"\n--- Findings ({len(findings)}) ---")
        for f in findings:
            severity = f['severity']
            marker = {'CRITICAL': '!!!', 'HIGH': '!!', 'MEDIUM': '!', 'LOW': '.'}.get(severity, '?')
            print(f"\n  [{marker}] {f['type']}: {f['message']}")
            if 'details' in f:
                for d in f['details']:
                    print(f"      {d}")
    else:
        print(f"\n--- No findings ---")

    # Timeline (verbose mode)
    if verbose and timeline:
        print(f"\n--- Timeline ({len(timeline)} events) ---")
        for ts, etype, detail in timeline:
            t = ts_str(ts)
            label = f"{etype:>12}"
            print(f"  {t}  {label}  {detail}")

    print()


def main():
    parser = argparse.ArgumentParser(description='Audit updates.jsonl for agent/asdaaas behavior')
    parser.add_argument('--agent', '-a', help='Agent name (e.g., Trip, Jr, Sr)')
    parser.add_argument('--session-dir', '-s', help='Direct path to session directory')
    parser.add_argument('--last', '-l', help='Duration to look back (e.g., 2h, 30m, 1d)')
    parser.add_argument('--from', dest='from_time', help='Start time (e.g., "2026-06-22 07:00" or "07:00")')
    parser.add_argument('--to', dest='to_time', help='End time (e.g., "2026-06-22 08:00" or "08:00")')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show full timeline')
    args = parser.parse_args()

    if not args.agent and not args.session_dir:
        parser.error('Either --agent or --session-dir is required')

    if args.session_dir:
        session_dir = Path(args.session_dir)
    else:
        session_dir = find_session_dir(args.agent)

    updates_path = session_dir / 'updates.jsonl'
    if not updates_path.exists():
        print(f"No updates.jsonl in {session_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Session: {session_dir.name}", file=sys.stderr)
    print(f"Updates: {updates_path} ({updates_path.stat().st_size / 1024 / 1024:.1f} MB)", file=sys.stderr)

    # Determine time bounds
    now = int(time.time())
    if args.last:
        duration = parse_duration(args.last)
        start_ts = now - duration
        end_ts = now
    elif args.from_time:
        start_ts = parse_time(args.from_time)
        end_ts = parse_time(args.to_time) if args.to_time else now
    else:
        # Default: last 4 hours
        start_ts = now - 4 * 3600
        end_ts = now

    events = parse_updates(updates_path, start_ts, end_ts)
    findings = analyze(events, verbose=args.verbose)
    timeline = build_timeline(events) if args.verbose else []

    print_report(events, findings, timeline, args.verbose, start_ts, end_ts)


if __name__ == '__main__':
    main()
