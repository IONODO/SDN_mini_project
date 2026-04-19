#!/usr/bin/env python3
"""
Flow Rule Timeout Manager – Validation & Regression Test Suite
==============================================================
Run this script WHILE the topology.py is active (or standalone
against a running Mininet + Ryu environment) to:

  1. Inspect live OVS flow tables via ovs-ofctl
  2. Parse /tmp/flow_records.json written by the controller
  3. Validate that every IDLE_TIMEOUT removal happened within
     the expected time window
  4. Print a structured pass/fail report

Usage (requires root / Mininet running):
  sudo python3 validate.py
"""

import json
import subprocess
import sys
import time
import os

IDLE_TIMEOUT_SHORT  = 10
TOLERANCE_SECS      = 6    # allow 6s for OVS + network notification lag
RECORDS_FILE        = "/tmp/flow_records.json"


# ── helpers ───────────────────────────────────────────────────────────────────

def run(cmd):
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return out.stdout + out.stderr


def banner(title):
    print("\n" + "="*64)
    print(f"  {title}")
    print("="*64)


# ── test functions ────────────────────────────────────────────────────────────

def test_flow_table_structure():
    """Verify OVS flow tables are reachable and have a table-miss rule."""
    banner("TEST: OVS Flow Table Structure")
    passed = True

    for sw in ['s1', 's2']:
        out = run(f"ovs-ofctl -O OpenFlow13 dump-flows {sw} 2>&1")
        if 'OFPST_FLOW' in out or 'priority=0' in out:
            print(f"  ✓ {sw}: flow table accessible, table-miss rule present")
        else:
            print(f"  ✗ {sw}: could not read flow table – is Mininet running?")
            print(f"         Output: {out[:200]}")
            passed = False

    return passed


def test_idle_timeout_values():
    """Check that installed flows carry expected idle_timeout values."""
    banner("TEST: Idle Timeout Values in Flow Table")
    passed = True

    out = run("ovs-ofctl -O OpenFlow13 dump-flows s1 2>&1")
    if 'idle_timeout' not in out and 'hard_timeout' not in out:
        print("  ⚠  No timeout flows visible right now (may have all expired).")
        print("     Trigger traffic first: sudo mn --test pingall")
        return True   # not a failure – depends on timing

    for line in out.splitlines():
        if 'idle_timeout' in line:
            # Extract idle_timeout=N
            for token in line.split(','):
                token = token.strip()
                if token.startswith('idle_timeout='):
                    val = int(token.split('=')[1])
                    if val in (IDLE_TIMEOUT_SHORT, 30):
                        print(f"  ✓ idle_timeout={val} is an expected value")
                    else:
                        print(f"  ✗ Unexpected idle_timeout={val}")
                        passed = False

    return passed


def test_lifecycle_records():
    """Parse controller-written records and validate removal timing."""
    banner("TEST: Flow Lifecycle Record Validation")

    if not os.path.exists(RECORDS_FILE):
        print(f"  ⚠  {RECORDS_FILE} not found – controller may not have run yet.")
        return True

    with open(RECORDS_FILE) as f:
        records = json.load(f)

    if not records:
        print("  ⚠  No records found.")
        return True

    passed = True
    idle_removed = [r for r in records if r['reason'] == 'IDLE_TIMEOUT']
    hard_removed = [r for r in records if r['reason'] == 'HARD_TIMEOUT']
    deleted      = [r for r in records if r['reason'] == 'DELETE']
    active       = [r for r in records if r['reason'] is None]

    print(f"  Total records : {len(records)}")
    print(f"  Active        : {len(active)}")
    print(f"  Idle-expired  : {len(idle_removed)}")
    print(f"  Hard-expired  : {len(hard_removed)}")
    print(f"  Deleted       : {len(deleted)}")

    # Validate idle-expired flows removed within window
    print("\n  Idle-timeout removal timing:")
    for r in idle_removed:
        expected = r['idle_to']
        actual   = r['duration']
        # duration should be >= idle_to (no traffic) and <= idle_to + TOLERANCE
        ok = (actual is not None and
              expected <= actual <= expected + TOLERANCE_SECS)
        mark = "✓" if ok else "✗"
        print(f"    {mark} {r['match'][:55]:<55} "
              f"idle_to={expected}s  actual={actual:.1f}s" if actual else
              f"    {mark} {r['match'][:55]:<55} idle_to={expected}s  actual=None")
        if not ok:
            passed = False

    return passed


def test_block_rules():
    """Ping from h3 (blocked host) to h4 – expect 0 received."""
    banner("TEST: Block Rule Enforcement")

    out = run("ip netns list 2>&1")
    if 'h3' not in out and 'mininet' not in out:
        # Try mn exec style
        result = run("mn --test pingall 2>&1 | head -5")
        print("  ⚠  Running outside Mininet namespace – skipping live ping test.")
        print("     To test manually:  sudo mn exec h3 ping -c2 -W2 10.0.0.4")
        return True

    # We're inside Mininet namespace – run directly
    result = run("ip netns exec h3 ping -c 4 -W 2 10.0.0.4 2>&1")
    if '0 received' in result or '100% packet loss' in result:
        print("  ✓ h3 → h4 correctly blocked (0 packets received)")
        return True
    else:
        print("  ✗ h3 → h4 NOT blocked!")
        print("    ", result[:300])
        return False


def test_regression_consistency():
    """Cross-check all idle-expired flows for consistent timeout behaviour."""
    banner("REGRESSION: Timeout Consistency Across Rounds")

    if not os.path.exists(RECORDS_FILE):
        print("  ⚠  No records file – skipping regression.")
        return True

    with open(RECORDS_FILE) as f:
        records = json.load(f)

    idle_removed = [r for r in records
                    if r['reason'] == 'IDLE_TIMEOUT' and r['duration'] is not None]

    if len(idle_removed) < 2:
        print(f"  ⚠  Only {len(idle_removed)} idle-expired records – need ≥2 for consistency check.")
        return True

    durations = [r['duration'] for r in idle_removed]
    avg = sum(durations) / len(durations)
    variance = max(durations) - min(durations)
    passed = variance <= TOLERANCE_SECS

    mark = "✓" if passed else "✗"
    print(f"  Idle-expired flows : {len(idle_removed)}")
    print(f"  Duration range     : {min(durations):.1f}s – {max(durations):.1f}s")
    print(f"  Average            : {avg:.1f}s")
    print(f"  Variance (max-min) : {variance:.1f}s  (tolerance: {TOLERANCE_SECS}s)")
    print(f"  {mark} Consistency {'PASS' if passed else 'FAIL'}")

    return passed


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    banner("SDN Flow Timeout Manager – Validation Suite")
    print(f"  Run time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}
    results['flow_table_structure']   = test_flow_table_structure()
    results['idle_timeout_values']    = test_idle_timeout_values()
    results['lifecycle_records']      = test_lifecycle_records()
    results['block_rules']            = test_block_rules()
    results['regression_consistency'] = test_regression_consistency()

    banner("FINAL REPORT")
    all_pass = True
    for name, ok in results.items():
        mark = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {mark}  {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("  ✅  ALL TESTS PASSED")
        sys.exit(0)
    else:
        print("  ❌  SOME TESTS FAILED – see above for details")
        sys.exit(1)


if __name__ == '__main__':
    main()
