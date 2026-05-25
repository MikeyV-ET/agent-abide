#!/usr/bin/env python3
"""
MikeyV Stress Test Adapter -- Configurable message generator for hub testing.
=============================================================================
Generates N messages at a configurable rate, writes them to the hub inbox
via adapter_api. Useful for:
  - Testing hub throughput and latency
  - Finding leader callback limits
  - Validating adapter_api collision safety under load
  - Benchmarking end-to-end delivery

Usage:
  python3 stress_test_adapter.py --count 10 --rate 1.0
  python3 stress_test_adapter.py --count 100 --rate 10.0 --target Sr
  python3 stress_test_adapter.py --count 50 --rate 5.0 --target broadcast --dry-run
  python3 stress_test_adapter.py --burst 20  # send 20 messages as fast as possible

Modes:
  --rate N      Send N messages per second (sustained)
  --burst N     Send N messages as fast as possible, then stop
  --ramp        Start slow, increase rate over time
"""

import argparse
import json
import os
import sys
import time
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

ADAPTER_NAME = "stress_test"


def run_stress_test(count, rate, target, dry_run=False, payload_size=50):
    """Run a sustained-rate stress test."""
    adapter_api.ensure_dirs(ADAPTER_NAME)
    adapter_api.register_adapter(ADAPTER_NAME, capabilities=["send"], config={
        "mode": "sustained",
        "count": count,
        "rate": rate,
        "target": target,
    })

    interval = 1.0 / rate if rate > 0 else 0
    print(f"[stress] Sustained mode: {count} messages at {rate}/s to {target}")
    print(f"[stress] Interval: {interval*1000:.1f}ms between messages")
    if dry_run:
        print("[stress] DRY RUN -- no messages will be written")

    write_times = []
    sent = 0
    start = time.time()

    for i in range(count):
        msg_text = f"Stress test message {i+1}/{count} (payload: {'X' * payload_size})"

        t0 = time.time()
        if not dry_run:
            adapter_api.write_message(
                to=target,
                text=msg_text,
                adapter=ADAPTER_NAME,
                sender="stress_tester",
                meta={"seq": i+1, "total": count, "mode": "sustained"},
            )
        t1 = time.time()
        write_times.append((t1 - t0) * 1000)  # ms

        sent += 1
        if sent % 10 == 0 or sent == count:
            elapsed = time.time() - start
            actual_rate = sent / elapsed if elapsed > 0 else 0
            print(f"[stress] Sent {sent}/{count} ({actual_rate:.1f}/s actual)")

        # Rate limiting
        if interval > 0 and i < count - 1:
            expected_time = start + (i + 1) * interval
            sleep_time = expected_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)

    elapsed = time.time() - start
    print_results(sent, elapsed, write_times, dry_run)

    adapter_api.deregister_adapter(ADAPTER_NAME)


def run_burst_test(count, target, dry_run=False, payload_size=50):
    """Send N messages as fast as possible."""
    adapter_api.ensure_dirs(ADAPTER_NAME)
    adapter_api.register_adapter(ADAPTER_NAME, capabilities=["send"], config={
        "mode": "burst",
        "count": count,
        "target": target,
    })

    print(f"[stress] Burst mode: {count} messages to {target} (max speed)")
    if dry_run:
        print("[stress] DRY RUN -- no messages will be written")

    write_times = []
    start = time.time()

    for i in range(count):
        msg_text = f"Burst test message {i+1}/{count} (payload: {'X' * payload_size})"

        t0 = time.time()
        if not dry_run:
            adapter_api.write_message(
                to=target,
                text=msg_text,
                adapter=ADAPTER_NAME,
                sender="stress_tester",
                meta={"seq": i+1, "total": count, "mode": "burst"},
            )
        t1 = time.time()
        write_times.append((t1 - t0) * 1000)

    elapsed = time.time() - start
    print_results(count, elapsed, write_times, dry_run)

    adapter_api.deregister_adapter(ADAPTER_NAME)


def run_ramp_test(count, max_rate, target, dry_run=False, payload_size=50):
    """Start at 1/s, ramp up to max_rate over the course of count messages."""
    adapter_api.ensure_dirs(ADAPTER_NAME)
    adapter_api.register_adapter(ADAPTER_NAME, capabilities=["send"], config={
        "mode": "ramp",
        "count": count,
        "max_rate": max_rate,
        "target": target,
    })

    print(f"[stress] Ramp mode: {count} messages, 1/s -> {max_rate}/s to {target}")
    if dry_run:
        print("[stress] DRY RUN -- no messages will be written")

    write_times = []
    start = time.time()

    for i in range(count):
        # Linear ramp from 1/s to max_rate/s
        progress = i / max(count - 1, 1)
        current_rate = 1.0 + progress * (max_rate - 1.0)
        interval = 1.0 / current_rate

        msg_text = f"Ramp test message {i+1}/{count} at {current_rate:.1f}/s (payload: {'X' * payload_size})"

        t0 = time.time()
        if not dry_run:
            adapter_api.write_message(
                to=target,
                text=msg_text,
                adapter=ADAPTER_NAME,
                sender="stress_tester",
                meta={"seq": i+1, "total": count, "mode": "ramp", "rate": round(current_rate, 1)},
            )
        t1 = time.time()
        write_times.append((t1 - t0) * 1000)

        if (i + 1) % 10 == 0 or i == count - 1:
            elapsed = time.time() - start
            actual_rate = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"[stress] Sent {i+1}/{count} (target {current_rate:.1f}/s, actual avg {actual_rate:.1f}/s)")

        if i < count - 1:
            time.sleep(max(0, interval - (time.time() - t0)))

    elapsed = time.time() - start
    print_results(count, elapsed, write_times, dry_run)

    adapter_api.deregister_adapter(ADAPTER_NAME)


def print_results(count, elapsed, write_times, dry_run):
    """Print benchmark results."""
    print(f"\n{'=' * 50}")
    print(f"Stress Test Results {'(DRY RUN)' if dry_run else ''}")
    print(f"{'=' * 50}")
    print(f"Messages sent:    {count}")
    print(f"Total time:       {elapsed:.3f}s")
    print(f"Throughput:       {count/elapsed:.1f} msg/s")
    print(f"Write latency:")
    print(f"  Mean:           {statistics.mean(write_times):.3f}ms")
    print(f"  Median:         {statistics.median(write_times):.3f}ms")
    print(f"  P95:            {sorted(write_times)[int(len(write_times)*0.95)]:.3f}ms")
    print(f"  P99:            {sorted(write_times)[int(len(write_times)*0.99)]:.3f}ms")
    print(f"  Max:            {max(write_times):.3f}ms")
    print(f"  Min:            {min(write_times):.3f}ms")

    # Write results to file
    results = {
        "ts": time.time(),
        "count": count,
        "elapsed_s": round(elapsed, 3),
        "throughput_per_s": round(count / elapsed, 1),
        "write_latency_ms": {
            "mean": round(statistics.mean(write_times), 3),
            "median": round(statistics.median(write_times), 3),
            "p95": round(sorted(write_times)[int(len(write_times)*0.95)], 3),
            "p99": round(sorted(write_times)[int(len(write_times)*0.99)], 3),
            "max": round(max(write_times), 3),
            "min": round(min(write_times), 3),
        },
        "dry_run": dry_run,
    }
    results_path = os.path.expanduser("~/asdaaas/stress_test_results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {results_path}")


def main():
    parser = argparse.ArgumentParser(description="MikeyV Stress Test Adapter")
    parser.add_argument("--count", "-n", type=int, default=10,
                        help="Number of messages to send (default: 10)")
    parser.add_argument("--rate", "-r", type=float, default=1.0,
                        help="Messages per second for sustained mode (default: 1.0)")
    parser.add_argument("--target", "-t", default="Sr",
                        help="Target agent or 'broadcast' (default: Sr)")
    parser.add_argument("--burst", type=int, metavar="N",
                        help="Burst mode: send N messages as fast as possible")
    parser.add_argument("--ramp", action="store_true",
                        help="Ramp mode: increase rate from 1/s to --rate over --count messages")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate without writing messages")
    parser.add_argument("--payload-size", type=int, default=50,
                        help="Size of payload padding in chars (default: 50)")
    args = parser.parse_args()

    if args.burst:
        run_burst_test(args.burst, args.target, args.dry_run, args.payload_size)
    elif args.ramp:
        run_ramp_test(args.count, args.rate, args.target, args.dry_run, args.payload_size)
    else:
        run_stress_test(args.count, args.rate, args.target, args.dry_run, args.payload_size)


if __name__ == "__main__":
    main()
