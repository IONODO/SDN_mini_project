# SDN Flow Rule Timeout Manager

**Course Project – SDN Mininet Simulation (Orange Problem)**

> An OpenFlow 1.3 controller built with Ryu that demonstrates the full lifecycle of flow rules — installation, traffic-based refresh, idle expiry, hard expiry, and explicit deletion — using a custom Mininet topology.

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Architecture Overview](#architecture-overview)
3. [Topology](#topology)
4. [How It Works](#how-it-works)
5. [Setup & Execution](#setup--execution)
6. [Test Scenarios](#test-scenarios)
7. [Validation & Regression Testing](#validation--regression-testing)
8. [Proof of Execution](#proof-of-execution)
9. [References](#references)

---

## Problem Statement

Traditional networks use static routing tables that require manual updates when topology or policy changes. Software-Defined Networking (SDN) decouples the control plane from the data plane, allowing a centralised controller to dynamically install, modify, and expire forwarding rules.

This project implements an **SDN Flow Rule Timeout Manager** that:

- Installs per-flow rules dynamically via `packet_in` handling
- Assigns **idle timeouts** (rules expire when no matching packets arrive for N seconds) and **hard timeouts** (rules expire unconditionally after N seconds regardless of traffic)
- Captures **flow-removed events** to log lifecycle data
- Enforces **security policies** by blocking specific hosts
- Runs **regression tests** to prove that timeout behaviour is deterministic across multiple rounds

---

## Architecture Overview

```
┌─────────────────────────────────────┐
│          Ryu Controller             │
│  timeout_manage_controller.py       │
│                                     │
│  ┌───────────────┐  ┌────────────┐  │
│  │  MAC Learning │  │  Block     │  │
│  │  Table        │  │  Rules     │  │
│  └───────────────┘  └────────────┘  │
│  ┌──────────────────────────────┐   │
│  │  FlowLifecycleRecord tracker │   │
│  │  → /tmp/flow_records.json    │   │
│  └──────────────────────────────┘   │
└────────────────┬────────────────────┘
                 │ OpenFlow 1.3
        ┌────────┴────────┐
        ▼                 ▼
      [ s1 ]  ────────  [ s2 ]
     /  |  \               |  \
   h1  h2  h3             h4  h5
```

**Key components:**

| File | Purpose |
|------|---------|
| `timeout_manage_controller.py` | Ryu controller – MAC learning, flow installation, lifecycle tracking |
| `topology.py` | Mininet topology + automated test scenarios |
| `validate.py` | Offline validation suite – parses flow records and OVS tables |

---

## Topology

```
    h1 (10.0.0.1) ─┐
    h2 (10.0.0.2) ─┤── s1 ──── s2 ──┬── h4 (10.0.0.4)
    h3 (10.0.0.3) ─┘                 └── h5 (10.0.0.5)
```

| Element | Detail |
|---------|--------|
| Hosts h1–h3 | Left LAN, connected to s1 at 100 Mbps / 2 ms |
| Hosts h4–h5 | Right LAN, connected to s2 at 100 Mbps / 2 ms |
| Trunk link | s1 ↔ s2 at 1 Gbps / 5 ms |
| Protocol | OpenFlow 1.3 |
| Controller | Ryu (remote, 127.0.0.1:6653) |

---

## How It Works

### Flow Rule Lifecycle

```
packet_in received
       │
       ▼
MAC learned / looked up
       │
  dst known?
  ┌────┴────┐
 YES        NO → FLOOD (no rule installed)
  │
  ▼
Detect L4 protocol
  ├─ TCP  → idle=10s, hard=60s
  ├─ UDP  → idle=10s, hard=60s
  └─ Other → idle=30s, hard=0 (no hard limit)
       │
       ▼
OFPFlowMod sent with OFPFF_SEND_FLOW_REM flag
       │
  [traffic flows normally through switch]
       │
  idle timeout fires?  OR  hard timeout fires?
       │                          │
       ▼                          ▼
  EventOFPFlowRemoved         EventOFPFlowRemoved
  reason=IDLE_TIMEOUT         reason=HARD_TIMEOUT
       │
       ▼
FlowLifecycleRecord updated → written to /tmp/flow_records.json
```

### Timeout Constants

| Constant | Value | Applied to |
|----------|-------|-----------|
| `IDLE_TIMEOUT_SHORT` | 10 s | TCP and UDP flows |
| `IDLE_TIMEOUT_NORMAL` | 30 s | ARP and other non-IP traffic |
| `HARD_TIMEOUT_MAX` | 60 s | TCP and UDP flows |
| Block rules | 0 / 0 | Persistent (never expire) |

---

## Setup & Execution

### Prerequisites

```bash
# Install Mininet
sudo apt-get install mininet

# Install Ryu
pip install ryu

# (Optional) Install iperf3 for throughput tests
sudo apt-get install iperf3
```

### 1. Start the Ryu Controller

```bash
ryu-manager timeout_manage_controller.py
```

> The controller listens on port 6653 by default.

### 2. Launch the Mininet Topology

In a **separate terminal**:

```bash
sudo python3 topology.py
```

This automatically runs:
- Scenario 1 (allowed vs blocked traffic)
- Scenario 2 (timeout lifecycle demo)
- Regression test suite (3 rounds)

To open an interactive Mininet CLI after tests:

```bash
sudo python3 topology.py --cli
```

### 3. Run the Validation Suite (optional)

In a third terminal while Mininet is still running:

```bash
sudo python3 validate.py
```

### Useful Manual Commands

```bash
# Dump flow table on s1
ovs-ofctl -O OpenFlow13 dump-flows s1

# Ping from h1 to h4
mininet> h1 ping h4

# iperf3 throughput test
mininet> h4 iperf3 -s &
mininet> h1 iperf3 -c 10.0.0.4 -t 20
```

---

## Test Scenarios

### Scenario 1 – Allowed vs Blocked Traffic

Tests that traffic forwarding works normally, and that the host configured in `BLOCKED_HOSTS` has its packets silently dropped.

**Expected result:**
- `h1 → h4`: ping succeeds, forwarding flow installed with `idle_timeout=10s`
- `h3 → h4`: 0 packets received (drop rule, highest priority)

### Scenario 2 – Flow Rule Timeout Lifecycle

Demonstrates the complete birth-to-death lifecycle of a flow:

1. Ping `h1 → h4` → flow installed
2. Wait 15 s (idle_timeout = 10 s) → flow removed, `FLOW_REMOVED` logged
3. Re-ping → new flow installed (proves rules are re-learned)
4. Run `iperf3` for 25 s → flow kept alive by traffic, eventually hits `hard_timeout=60s`

---

## Validation & Regression Testing

`validate.py` performs five checks:

| Test | What it checks |
|------|----------------|
| Flow table structure | OVS reachable, table-miss rule installed |
| Idle timeout values | Installed flows carry expected `idle_timeout` values |
| Lifecycle records | Every idle-expired flow removed within `idle_to + 6s` window |
| Block rule enforcement | h3 → h4 ping returns 0 packets |
| Regression consistency | Max variance across all idle-expired durations ≤ 6 s |

---

## Proof of Execution

### Flow Table (after initial ping)

> _Screenshot placeholder – paste your `ovs-ofctl dump-flows s1` output here_

![Flow table after ping](screenshots/flow_table_after_ping.png)

---

### Scenario 1 – Ping Results

> _Screenshot placeholder – terminal showing h1→h4 success and h3→h4 blocked_

![Scenario 1 ping results](screenshots/scenario1_ping.png)

---

### Scenario 2 – Timeout Lifecycle Logs

> _Screenshot placeholder – controller log showing FLOW INSTALLED → FLOW REMOVED (IDLE_TIMEOUT)_

![Timeout lifecycle log](screenshots/scenario2_timeout_log.png)

---

### iperf3 Throughput Results

> _Screenshot placeholder – iperf3 client output from h1_

![iperf3 results](screenshots/iperf3_results.png)

---

### Validation Suite Output

> _Screenshot placeholder – validate.py final report showing PASS/FAIL per test_

![Validation output](screenshots/validate_output.png)

---

### Wireshark Capture (optional)

> _Screenshot placeholder – Wireshark capture showing OpenFlow PACKET_IN / FLOW_MOD / FLOW_REMOVED messages_

![Wireshark capture](screenshots/wireshark_openflow.png)

---

## References

1. OpenFlow Switch Specification, Version 1.3.0 – Open Networking Foundation  
   https://opennetworking.org/wp-content/uploads/2014/10/openflow-spec-v1.3.0.pdf

2. Ryu SDN Framework Documentation  
   https://ryu.readthedocs.io/en/latest/

3. Mininet Documentation  
   http://mininet.org/

4. B. Lantz, B. Heller, N. McKeown – "A Network in a Laptop: Rapid Prototyping for Software-Defined Networks", HotNets '10

5. N. McKeown et al. – "OpenFlow: Enabling Innovation in Campus Networks", ACM SIGCOMM CCR, 2008
