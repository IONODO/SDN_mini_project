# SDN Flow Rule Timeout Manager

**Course Project – SDN Mininet Simulation (Orange Problem)**

> An OpenFlow 1.3 controller built with Ryu that demonstrates the full lifecycle of flow rules — installation, traffic-based refresh, idle expiry, and explicit deletion — using a custom Mininet topology.

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Architecture Overview](#architecture-overview)
3. [Topology](#topology)
4. [How It Works](#how-it-works)
5. [Setup & Execution](#setup--execution)
6. [Test Scenarios](#test-scenarios)
7. [Regression Testing](#regression-testing)
8. [Proof of Execution](#proof-of-execution)
9. [References](#references)

---

## Problem Statement

Traditional networks use static routing tables that require manual updates when topology or policy changes. Software-Defined Networking (SDN) decouples the control plane from the data plane, allowing a centralised controller to dynamically install, modify, and expire forwarding rules.

This project implements an **SDN Flow Rule Timeout Manager** that:

- Installs per-flow rules dynamically via `packet_in` handling
- Assigns **idle timeouts** (rules expire when no matching packets arrive for N seconds) and **hard timeouts** (rules expire unconditionally after N seconds)
- Captures **flow-removed events** to log lifecycle data
- Enforces **security policies** by blocking specific hosts
- Runs **regression tests** to prove that timeout behaviour is deterministic

---

## Architecture Overview

```
┌─────────────────────────────────────┐
│          Ryu Controller             │
│  timeout_manage_controller.py       │
│                                     │
│  ┌───────────────┐  ┌────────────┐  │
│  │  MAC Learning │  │   Block    │  │
│  │  Table        │  │   Rules    │  │
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

### Timeout Constants

| Constant | Value | Applied to |
|----------|-------|-----------|
| `IDLE_TIMEOUT_SHORT` | 10 s | TCP and UDP flows |
| `IDLE_TIMEOUT_NORMAL` | 30 s | ARP and other non-IP traffic |
| `HARD_TIMEOUT_MAX` | 60 s | TCP and UDP flows |
| Block rules | 0 / 0 | Persistent (never expire) |

### Flow Rule Lifecycle

1. **packet_in** arrives → MAC learned, destination looked up
2. If destination known → flow installed with `OFPFF_SEND_FLOW_REM` and per-protocol timeouts
3. Switch forwards traffic in the data plane
4. Idle or hard timeout fires → switch sends **FLOW_REMOVED** to controller
5. Controller logs removal reason, duration, and packet count to `/tmp/flow_records.json`

---

## Setup & Execution

### Prerequisites

```bash
sudo apt-get install mininet iperf3
pip install ryu
```

### 1. Start the Ryu Controller

```bash
ryu-manager timeout_manage_controller.py
```

### 2. Launch the Topology (separate terminal)

```bash
sudo python3 topology.py
```

This automatically runs Scenario 1, Scenario 2, and the regression suite.

### 3. Validate (optional, while Mininet is running)

```bash
sudo python3 validate.py
```

### Useful Manual Commands

```bash
# Dump flow table
ovs-ofctl -O OpenFlow13 dump-flows s1

# Ping test
mininet> h1 ping -c 4 h4

# Throughput test
mininet> h4 iperf3 -s &
mininet> h1 iperf3 -c 10.0.0.4 -t 20 -i 5
```

> **Note:** To enable host blocking, set `BLOCKED_HOSTS = ['10.0.0.3']` in the controller *before* starting Ryu. Block rules are installed at switch-connect time.

---

## Test Scenarios

### Scenario 1 – Allowed vs Blocked Traffic

- `h1 → h4`: ping succeeds, forwarding flow installed with `idle_timeout=30s`
- `h3 → h4`: dropped if `BLOCKED_HOSTS` is populated before controller startup

### Scenario 2 – Flow Rule Timeout Lifecycle

1. Ping `h1 → h4` → flow installed, visible in `ovs-ofctl dump-flows`
2. Wait 15 s → flow removed, `FLOW_REMOVED` logged with `reason=IDLE_TIMEOUT`
3. Re-ping → new flow installed (proves rules are re-learned after expiry)
4. Run `iperf3` for 25 s → flow kept alive by traffic until `hard_timeout=60s`

---

## Regression Testing

Three independent rounds each install a flow, wait `idle_timeout + 5 s`, then check the OVS table. All rounds must show the forwarding rule absent within the expected window.

**Result: PASS=3, FAIL=0** — all flows removed at ~16 s (expected 10 s ± 5 s).

---

## Proof of Execution

### Scenario 1 – Ping Results

![Scenario 1](screenshots/1.png)

h1→h4: 4/4 packets received. h3→h4 was not blocked because `BLOCKED_HOSTS` was empty at runtime — block rules must be configured before controller startup.

---

### Scenario 2 – Flow Table & Idle Timeout (Steps 1–2)

![Scenario 2 Steps 1-2](screenshots/2.png)

Flow table after initial ping shows `idle_timeout=30, send_flow_rem`. After 15 s the controller confirms forwarding flows were removed by idle timeout ✓

---

### Scenario 2 – Re-ping & iperf3 (Steps 3–4)

![Scenario 2 Steps 3-4](screenshots/3.png)

New flows installed after re-ping ✓. iperf3 was not installed in this environment — install with `sudo apt-get install iperf3`.

---

### Controller Log – MAC Learning & FLOW_REMOVED

![Controller log](screenshots/4.png)

Shows SWITCH CONNECTED → table-miss installed, MAC addresses learned, FLOW INSTALLED records with `idle=30s hard=0s`, and a FLOW REMOVED event with `reason=IDLE_TIMEOUT duration=35.6s`.

---

### Regression Suite – All Rounds PASS

![Regression tests](screenshots/5.png)

All 3 regression rounds passed. PASS=3 FAIL=0. All scenarios complete.

---

## References

1. Open Networking Foundation. *OpenFlow Switch Specification, Version 1.3.0*, 2012.  
   https://opennetworking.org/wp-content/uploads/2014/10/openflow-spec-v1.3.0.pdf

2. Ryu SDN Framework. *Ryu Documentation*.  
   https://ryu.readthedocs.io/en/latest/

3. Mininet Project. *Mininet: An Instant Virtual Network on your Laptop*.  
   http://mininet.org/

4. B. Lantz, B. Heller, N. McKeown. "A Network in a Laptop: Rapid Prototyping for Software-Defined Networks." *HotNets '10*, 2010.

5. N. McKeown et al. "OpenFlow: Enabling Innovation in Campus Networks." *ACM SIGCOMM Computer Communication Review*, 38(2):69–74, 2008.
