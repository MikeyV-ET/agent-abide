#!/usr/bin/env python3
"""
MikeyV Leader Crash Test -- Find the breaking point of the leader process.
==========================================================================
Connects to an isolated testbed leader and stress tests it with:
  1. Rapid connections (connect/register/initialize cycles)
  2. Rapid prompts to a session (if available)
  3. Concurrent connections (parallel clients)

SAFETY: Only use with the testbed leader, never production.
Set MIKEYV_LEADER_SOCK to point at the testbed.

Usage:
  MIKEYV_LEADER_SOCK=/home/eric/testbed/.grok/leader.sock python3 leader_crash_test.py --mode connections --count 50
  MIKEYV_LEADER_SOCK=/home/eric/testbed/.grok/leader.sock python3 leader_crash_test.py --mode concurrent --count 20
  MIKEYV_LEADER_SOCK=/home/eric/testbed/.grok/leader.sock python3 leader_crash_test.py --mode rapid-fire --count 100 --session-id <ID>
"""

import asyncio
import json
import os
import struct
import sys
import time
import argparse
import statistics

LEADER_SOCK = os.environ.get("MIKEYV_LEADER_SOCK", os.path.expanduser("~/.grok/leader.sock"))


async def send_frame(writer, payload):
    data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    frame = struct.pack('>I', len(data)) + data
    writer.write(frame)
    await writer.drain()


async def recv_frame(reader, timeout=10.0):
    length_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    length = struct.unpack('>I', length_bytes)[0]
    data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
    return json.loads(data.decode('utf-8'))


async def send_acp(writer, reader, req_id, method, params):
    """Send an ACP JSON-RPC message wrapped in leader envelope."""
    acp_msg = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }
    await send_frame(writer, {
        "type": "acp",
        "payload": json.dumps(acp_msg, separators=(',', ':'))
    })
    resp = await recv_frame(reader)
    if resp.get("type") == "acp":
        return json.loads(resp.get("payload", "{}"))
    return resp


async def do_handshake(reader, writer, req_id_start=0):
    """Full connect + register + initialize handshake. Returns client_id."""
    req_id = req_id_start

    # Register (leader protocol, not ACP)
    await send_frame(writer, {
        "type": "register",
        "client_type": "mikeyv-callback",
        "mode": "stdio",
        "capabilities": {
            "yolo_mode": False,
            "default_model": None,
            "client_version": "0.1.0 (crash-test)",
            "code_nav_enabled": False,
        }
    })
    resp = await recv_frame(reader)
    if resp.get("type") != "registered":
        # Wait for leader_ready if needed
        if not resp.get("ready", True):
            while True:
                frame = await recv_frame(reader)
                if frame.get("type") == "leader_ready":
                    break
    client_id = resp.get("client_id")
    print(f"  Registered as client {client_id}")
    req_id += 1

    # Initialize (ACP)
    result = await send_acp(writer, reader, req_id, "initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}
        }
    })
    print(f"  Initialized")
    req_id += 1

    # Authenticate (ACP)
    result = await send_acp(writer, reader, req_id, "authenticate", {
        "methodId": "cached_token"
    })
    print(f"  Authenticated")

    return client_id


async def test_connections(count, delay_ms):
    """Test rapid sequential connections."""
    print(f"\n=== CONNECTION STRESS TEST ===")
    print(f"Socket: {LEADER_SOCK}")
    print(f"Count: {count}, Delay: {delay_ms}ms between connections")

    times = []
    successes = 0
    failures = 0

    for i in range(count):
        t0 = time.time()
        try:
            reader, writer = await asyncio.open_unix_connection(LEADER_SOCK)
            await do_handshake(reader, writer, req_id_start=i*10)
            writer.close()
            await writer.wait_closed()
            t1 = time.time()
            times.append((t1 - t0) * 1000)
            successes += 1
            print(f"[{i+1}/{count}] OK ({times[-1]:.1f}ms)")
        except Exception as e:
            failures += 1
            t1 = time.time()
            times.append((t1 - t0) * 1000)
            print(f"[{i+1}/{count}] FAILED: {type(e).__name__}: {e}")

            # Check if leader is still alive
            if not os.path.exists(LEADER_SOCK):
                print(f"\n*** LEADER SOCKET GONE after {i+1} connections ***")
                break

        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    print_results("connections", count, successes, failures, times)


async def test_concurrent(count):
    """Test concurrent parallel connections."""
    print(f"\n=== CONCURRENT CONNECTION TEST ===")
    print(f"Socket: {LEADER_SOCK}")
    print(f"Concurrent clients: {count}")

    async def single_client(idx):
        t0 = time.time()
        try:
            reader, writer = await asyncio.open_unix_connection(LEADER_SOCK)
            await do_handshake(reader, writer, req_id_start=idx*10)
            # Hold connection open briefly
            await asyncio.sleep(0.5)
            writer.close()
            await writer.wait_closed()
            return ("ok", (time.time() - t0) * 1000)
        except Exception as e:
            return ("fail", (time.time() - t0) * 1000, str(e))

    results = await asyncio.gather(*[single_client(i) for i in range(count)], return_exceptions=True)

    successes = 0
    failures = 0
    times = []

    for i, r in enumerate(results):
        if isinstance(r, Exception):
            failures += 1
            print(f"  Client {i}: EXCEPTION: {r}")
        elif r[0] == "ok":
            successes += 1
            times.append(r[1])
            print(f"  Client {i}: OK ({r[1]:.1f}ms)")
        else:
            failures += 1
            times.append(r[1])
            print(f"  Client {i}: FAILED: {r[2]}")

    print_results("concurrent", count, successes, failures, times)


async def test_rapid_fire(count, session_id, delay_ms):
    """Rapid-fire prompts to a single session."""
    print(f"\n=== RAPID-FIRE PROMPT TEST ===")
    print(f"Socket: {LEADER_SOCK}")
    print(f"Session: {session_id}")
    print(f"Count: {count}, Delay: {delay_ms}ms between prompts")

    reader, writer = await asyncio.open_unix_connection(LEADER_SOCK)
    client_id = await do_handshake(reader, writer)

    times = []
    successes = 0
    failures = 0

    for i in range(count):
        t0 = time.time()
        try:
            req_id = 100 + i
            await send_frame(writer, {
                "type": "acp",
                "payload": json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "method": "session/prompt",
                    "params": {
                        "sessionId": session_id,
                        "prompt": f"[STRESS TEST {i+1}/{count}] Respond with exactly: noted",
                    }
                })
            })

            # Try to read response (with timeout)
            try:
                resp = await recv_frame(reader, timeout=30.0)
                t1 = time.time()
                times.append((t1 - t0) * 1000)
                successes += 1
                if (i + 1) % 5 == 0 or i == count - 1:
                    print(f"[{i+1}/{count}] OK ({times[-1]:.1f}ms)")
            except asyncio.TimeoutError:
                t1 = time.time()
                times.append((t1 - t0) * 1000)
                failures += 1
                print(f"[{i+1}/{count}] TIMEOUT")

        except Exception as e:
            failures += 1
            print(f"[{i+1}/{count}] FAILED: {type(e).__name__}: {e}")
            # Try to reconnect
            try:
                writer.close()
                reader, writer = await asyncio.open_unix_connection(LEADER_SOCK)
                client_id = await do_handshake(reader, writer)
                print(f"  Reconnected")
            except Exception:
                print(f"\n*** LEADER DEAD after {i+1} prompts ***")
                break

        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    print_results("rapid-fire", count, successes, failures, times)


def print_results(mode, total, successes, failures, times):
    print(f"\n{'=' * 50}")
    print(f"Leader Crash Test Results ({mode})")
    print(f"{'=' * 50}")
    print(f"Total attempts:   {total}")
    print(f"Successes:        {successes}")
    print(f"Failures:         {failures}")
    if times:
        print(f"Latency:")
        print(f"  Mean:           {statistics.mean(times):.1f}ms")
        print(f"  Median:         {statistics.median(times):.1f}ms")
        if len(times) >= 20:
            print(f"  P95:            {sorted(times)[int(len(times)*0.95)]:.1f}ms")
        print(f"  Max:            {max(times):.1f}ms")
        print(f"  Min:            {min(times):.1f}ms")

    # Check if leader survived
    alive = os.path.exists(LEADER_SOCK)
    print(f"\nLeader survived:  {'YES' if alive else 'NO *** CRASHED ***'}")

    # Save results
    results = {
        "ts": time.time(),
        "mode": mode,
        "total": total,
        "successes": successes,
        "failures": failures,
        "leader_survived": alive,
        "latency_ms": {
            "mean": round(statistics.mean(times), 1) if times else None,
            "median": round(statistics.median(times), 1) if times else None,
            "max": round(max(times), 1) if times else None,
        } if times else None,
    }
    results_path = os.path.expanduser("~/.grok/hub/leader_crash_test_results.json")
    os.makedirs(os.path.dirname(results_path), exist_ok=True)
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results saved to {results_path}")


def main():
    parser = argparse.ArgumentParser(description="MikeyV Leader Crash Test")
    parser.add_argument("--mode", choices=["connections", "concurrent", "rapid-fire"],
                        default="connections", help="Test mode")
    parser.add_argument("--count", "-n", type=int, default=10, help="Number of attempts")
    parser.add_argument("--delay", type=int, default=100, help="Delay between attempts in ms")
    parser.add_argument("--session-id", help="Session ID for rapid-fire mode")
    args = parser.parse_args()

    if args.mode == "rapid-fire" and not args.session_id:
        print("ERROR: --session-id required for rapid-fire mode")
        sys.exit(1)

    print(f"Leader socket: {LEADER_SOCK}")

    if args.mode == "connections":
        asyncio.run(test_connections(args.count, args.delay))
    elif args.mode == "concurrent":
        asyncio.run(test_concurrent(args.count))
    elif args.mode == "rapid-fire":
        asyncio.run(test_rapid_fire(args.count, args.session_id, args.delay))


if __name__ == "__main__":
    main()
