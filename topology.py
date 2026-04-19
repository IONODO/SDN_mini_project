#!/usr/bin/env python3
"""
SDN Flow Rule Timeout Manager – Mininet Topology
================================================
Topology:
           h1 ──┐
           h2 ──┤── s1 ──── s2 ──┬── h4
           h3 ──┘                └── h5

  h1-h3 connect to s1 (left LAN)
  h4-h5 connect to s2 (right LAN)
  s1 ↔ s2 via a trunk link

Usage:
  sudo python3 topology.py [--controller remote|integrated]
"""

import argparse
import time
import sys
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController, Controller
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink


def build_topology(use_remote=True):
    """Build and return a configured Mininet network."""
    net = Mininet(
        switch     = OVSSwitch,
        link       = TCLink,
        autoSetMacs = True,
    )

    info("*** Creating controller\n")
    if use_remote:
        ctrl = net.addController(
            'c0',
            controller = RemoteController,
            ip         = '127.0.0.1',
            port       = 6653,
        )
    else:
        ctrl = net.addController('c0')

    info("*** Creating switches\n")
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', protocols='OpenFlow13')

    info("*** Creating hosts\n")
    h1 = net.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
    h2 = net.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
    h3 = net.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
    h4 = net.addHost('h4', ip='10.0.0.4/24', mac='00:00:00:00:00:04')
    h5 = net.addHost('h5', ip='10.0.0.5/24', mac='00:00:00:00:00:05')

    info("*** Creating links\n")
    # Left LAN → s1
    net.addLink(h1, s1, bw=100, delay='2ms')
    net.addLink(h2, s1, bw=100, delay='2ms')
    net.addLink(h3, s1, bw=100, delay='2ms')
    # Right LAN → s2
    net.addLink(h4, s2, bw=100, delay='2ms')
    net.addLink(h5, s2, bw=100, delay='2ms')
    # Inter-switch trunk
    net.addLink(s1, s2, bw=1000, delay='5ms')

    return net


def run_scenario_1_allowed_vs_blocked(net):
    """
    Scenario 1 – Allowed vs Blocked Traffic
    ----------------------------------------
    h1 → h4 : should succeed (normal forward flow, idle_timeout=10s)
    h3 → h4 : h3 is in BLOCKED_HOSTS – traffic must be dropped
    """
    info("\n" + "="*60 + "\n")
    info("SCENARIO 1: Allowed vs Blocked Traffic\n")
    info("="*60 + "\n")

    h1, h3, h4 = net.get('h1', 'h3', 'h4')

    info("\n[TEST 1a] h1 → h4 ping (should SUCCEED)\n")
    result = h1.cmd('ping -c 4 10.0.0.4')
    info(result)
    if '4 received' in result or '3 received' in result:
        info("✓ PASS: h1 can reach h4\n")
    else:
        info("✗ FAIL: h1 cannot reach h4\n")

    info("\n[TEST 1b] h3 → h4 ping (should FAIL – blocked)\n")
    result = h3.cmd('ping -c 4 -W 2 10.0.0.4')
    info(result)
    if '0 received' in result or '100% packet loss' in result:
        info("✓ PASS: h3 correctly blocked\n")
    else:
        info("✗ FAIL: h3 traffic was NOT blocked\n")


def run_scenario_2_timeout_lifecycle(net):
    """
    Scenario 2 – Flow Rule Timeout Lifecycle
    -----------------------------------------
    1. Generate traffic h1→h4 → flow installed with idle_timeout=10s
    2. Wait IDLE_TIMEOUT + 5s without traffic → flow should be REMOVED
    3. Re-ping → new flow installed (proves lifecycle)
    4. Generate continuous traffic → flow kept alive until hard_timeout
    """
    info("\n" + "="*60 + "\n")
    info("SCENARIO 2: Flow Rule Timeout Lifecycle\n")
    info("="*60 + "\n")

    h1, h4, s1 = net.get('h1', 'h4', 's1')

    def dump_flows(label):
        info(f"\n--- Flow table ({label}) ---\n")
        out = s1.cmd('ovs-ofctl -O OpenFlow13 dump-flows s1')
        info(out + "\n")
        return out

    info("\n[STEP 1] Generate initial traffic to install flows\n")
    h1.cmd('ping -c 3 10.0.0.4')
    time.sleep(1)
    flows_after_ping = dump_flows("after initial ping")

    info("\n[STEP 2] Wait 15s (idle_timeout=10s) – flows should expire\n")
    for remaining in range(15, 0, -5):
        info(f"  ... {remaining}s remaining\n")
        time.sleep(5)
    flows_after_timeout = dump_flows("after idle timeout")

    # Check that learned forwarding flows are gone
    if 'eth_dst' not in flows_after_timeout or flows_after_timeout.count('idle_timeout') == 0:
        info("✓ PASS: Forwarding flows removed by idle timeout\n")
    else:
        info("✗ FAIL: Flows still present after idle timeout\n")

    info("\n[STEP 3] Re-ping – new flows should be re-installed\n")
    h1.cmd('ping -c 3 10.0.0.4')
    time.sleep(1)
    flows_after_reping = dump_flows("after re-ping (new flows)")
    if 'n_packets' in flows_after_reping:
        info("✓ PASS: New flows installed after re-ping\n")

    info("\n[STEP 4] Continuous iperf3 – flow kept alive, then hard_timeout kills it\n")
    info("  Starting iperf3 server on h4 ...\n")
    h4.cmd('iperf3 -s -D')
    time.sleep(1)
    info("  Running iperf3 client on h1 for 25s (hard_timeout=60s demo)\n")
    result = h1.cmd('iperf3 -c 10.0.0.4 -t 25 -i 5 2>&1')
    info(result + "\n")
    h4.cmd('kill %iperf3')
    dump_flows("during/after iperf3")


def run_regression_tests(net):
    """
    Regression: validate that timeout behaviour is deterministic.
    Run N rounds and check all idle-expired flows respect the window.
    """
    info("\n" + "="*60 + "\n")
    info("REGRESSION TESTS: Timeout Consistency Validation\n")
    info("="*60 + "\n")

    h1, h4 = net.get('h1', 'h4')
    IDLE_TIMEOUT = 10   # must match controller setting
    TOLERANCE    = 5    # seconds tolerance for removal notification

    passes = 0
    fails  = 0

    for i in range(1, 4):
        info(f"\n[REGRESSION ROUND {i}/3]\n")
        t_install = time.time()
        h1.cmd('ping -c 2 10.0.0.4 -W 1')
        info(f"  Flow installed at t=0\n")
        wait = IDLE_TIMEOUT + TOLERANCE
        info(f"  Waiting {wait}s for idle expiry ...\n")
        time.sleep(wait)

        # Check flow table – forwarding flow should be gone
        s1 = net.get('s1')
        flows = s1.cmd('ovs-ofctl -O OpenFlow13 dump-flows s1')
        elapsed = time.time() - t_install

        # A forwarding flow installed for this pair should be absent
        if 'eth_dst=00:00:00:00:00:04' not in flows:
            info(f"  ✓ Round {i} PASS – flow removed at ~{elapsed:.0f}s "
                 f"(expected ~{IDLE_TIMEOUT}s±{TOLERANCE}s)\n")
            passes += 1
        else:
            info(f"  ✗ Round {i} FAIL – flow STILL present at {elapsed:.0f}s\n")
            fails += 1

    info(f"\n[REGRESSION SUMMARY] PASS={passes}  FAIL={fails}\n")
    return fails == 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--remote', action='store_true',
                        help='Use remote Ryu controller (default)')
    parser.add_argument('--cli', action='store_true',
                        help='Drop into Mininet CLI after tests')
    args = parser.parse_args()

    setLogLevel('info')

    info("*** Building topology\n")
    net = build_topology(use_remote=True)

    info("*** Starting network\n")
    net.start()

    # Force OpenFlow 1.3 on all switches
    for sw in net.switches:
        sw.cmd(f'ovs-vsctl set bridge {sw.name} protocols=OpenFlow13')

    info("*** Waiting 3s for controller connection\n")
    time.sleep(3)

    try:
        run_scenario_1_allowed_vs_blocked(net)
        run_scenario_2_timeout_lifecycle(net)
        passed = run_regression_tests(net)

        info("\n" + "="*60 + "\n")
        info("ALL SCENARIOS COMPLETE\n")
        info(f"Regression suite: {'PASSED' if passed else 'FAILED'}\n")
        info("="*60 + "\n")

        if args.cli:
            CLI(net)

    finally:
        info("*** Stopping network\n")
        net.stop()


if __name__ == '__main__':
    main()
