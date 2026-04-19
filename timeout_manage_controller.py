"""
SDN Flow Rule Timeout Manager - Ryu Controller
============================================
Demonstrates:
  - Controller-switch interaction via OpenFlow 1.3
  - Flow rule design with match-action pairs
  - Idle timeout / hard timeout lifecycle management
  - packet_in handling with MAC learning
  - Flow-removed event capture for lifecycle logging
  - Per-flow statistics tracking
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, udp
from ryu.lib import hub
import time
import logging
import json
import os

LOG = logging.getLogger('timeout_manager')
LOG.setLevel(logging.DEBUG)

# ── tuneable constants ────────────────────────────────────────────────────────
IDLE_TIMEOUT_SHORT  = 10   # seconds – used for "short-lived" demo flows
IDLE_TIMEOUT_NORMAL = 30   # seconds – default learned flows
HARD_TIMEOUT_MAX    = 60   # seconds – absolute ceiling regardless of traffic
BLOCKED_HOSTS       = []   # e.g. ['10.0.0.3'] – hosts whose traffic is dropped
PRIORITY_BLOCK      = 100
PRIORITY_FORWARD    = 10
PRIORITY_DEFAULT    = 1
# ─────────────────────────────────────────────────────────────────────────────


class FlowLifecycleRecord:
    """Tracks one flow's birth → death for regression validation."""

    def __init__(self, dpid, match_desc, idle_to, hard_to):
        self.dpid        = dpid
        self.match_desc  = match_desc
        self.idle_to     = idle_to
        self.hard_to     = hard_to
        self.installed   = time.time()
        self.removed     = None
        self.reason      = None
        self.duration    = None

    def mark_removed(self, reason, duration):
        self.removed  = time.time()
        self.reason   = reason
        self.duration = duration

    def to_dict(self):
        return {
            'dpid'       : self.dpid,
            'match'      : self.match_desc,
            'idle_to'    : self.idle_to,
            'hard_to'    : self.hard_to,
            'installed'  : round(self.installed, 2),
            'removed'    : round(self.removed, 2) if self.removed else None,
            'duration'   : round(self.duration,  2) if self.duration else None,
            'reason'     : self.reason,
        }


class TimeoutManagerController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # dpid → {mac → port}
        self.mac_to_port = {}
        # list of FlowLifecycleRecord
        self.flow_records = []
        # periodic stats printer
        self._monitor_thread = hub.spawn(self._monitor_loop)
        LOG.info("=== Timeout Manager Controller started ===")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _add_flow(self, datapath, priority, match, actions,
                  idle_timeout=0, hard_timeout=0, record_desc=None):
        ofp   = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]

        # Ask the switch to send FLOW_REMOVED messages
        flags = ofp.OFPFF_SEND_FLOW_REM

        mod = parser.OFPFlowMod(
            datapath     = datapath,
            priority     = priority,
            match        = match,
            instructions = inst,
            idle_timeout = idle_timeout,
            hard_timeout = hard_timeout,
            flags        = flags,
        )
        datapath.send_msg(mod)

        if record_desc:
            rec = FlowLifecycleRecord(
                dpid     = datapath.id,
                match_desc = record_desc,
                idle_to  = idle_timeout,
                hard_to  = hard_timeout,
            )
            self.flow_records.append(rec)
            LOG.info("[FLOW INSTALLED] dpid=%s  match=%s  idle=%ds  hard=%ds",
                     datapath.id, record_desc, idle_timeout, hard_timeout)

    def _delete_flow_by_match(self, datapath, match):
        """Explicitly delete a flow (used by regression test helper)."""
        ofp    = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(
            datapath  = datapath,
            command   = ofp.OFPFC_DELETE,
            out_port  = ofp.OFPP_ANY,
            out_group = ofp.OFPG_ANY,
            match     = match,
        )
        datapath.send_msg(mod)

    # ── OpenFlow event handlers ───────────────────────────────────────────────

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """Install table-miss rule: send unknown packets to controller."""
        datapath = ev.msg.datapath
        ofp      = datapath.ofproto
        parser   = datapath.ofproto_parser

        match   = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER,
                                          ofp.OFPCML_NO_BUFFER)]
        # Table-miss has no timeout – it must persist
        self._add_flow(datapath, 0, match, actions)
        LOG.info("[SWITCH CONNECTED] dpid=%s – table-miss rule installed", datapath.id)

        # ── Install block rules for configured hosts ───────────────────────
        for blocked_ip in BLOCKED_HOSTS:
            bmatch = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                     ipv4_src=blocked_ip)
            self._add_flow(datapath, PRIORITY_BLOCK, bmatch, [],
                           idle_timeout=0, hard_timeout=0,
                           record_desc=f"BLOCK src={blocked_ip}")
            LOG.warning("[SECURITY] Blocking all traffic from %s on dpid=%s",
                        blocked_ip, datapath.id)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg      = ev.msg
        datapath = msg.datapath
        ofp      = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']

        pkt  = packet.Packet(msg.data)
        eth  = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return   # ignore LLDP

        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        # ── MAC learning ──────────────────────────────────────────────────
        self.mac_to_port.setdefault(dpid, {})
        if src not in self.mac_to_port[dpid]:
            self.mac_to_port[dpid][src] = in_port
            LOG.debug("[MAC LEARN] dpid=%s  %s → port %s", dpid, src, in_port)

        out_port = self.mac_to_port[dpid].get(dst, ofp.OFPP_FLOOD)

        actions = [parser.OFPActionOutput(out_port)]

        # ── Install forward flow with idle timeout ─────────────────────────
        if out_port != ofp.OFPP_FLOOD:
            # Choose timeout based on traffic type
            ip_pkt  = pkt.get_protocol(ipv4.ipv4)
            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)

            if tcp_pkt:
                # TCP: short idle timeout (reset on each segment)
                idle = IDLE_TIMEOUT_SHORT
                hard = HARD_TIMEOUT_MAX
                proto_label = "TCP"
            elif udp_pkt:
                idle = IDLE_TIMEOUT_SHORT
                hard = HARD_TIMEOUT_MAX
                proto_label = "UDP"
            else:
                idle = IDLE_TIMEOUT_NORMAL
                hard = 0   # no hard timeout for ARP / other
                proto_label = "OTHER"

            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            desc  = f"{proto_label} {src}→{dst} port={in_port}"
            self._add_flow(datapath, PRIORITY_FORWARD, match, actions,
                           idle_timeout=idle,
                           hard_timeout=hard,
                           record_desc=desc)

        # ── Send packet out ───────────────────────────────────────────────
        data = None
        if msg.buffer_id == ofp.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath  = datapath,
            buffer_id = msg.buffer_id,
            in_port   = in_port,
            actions   = actions,
            data      = data,
        )
        datapath.send_msg(out)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        """Capture flow-removed events for lifecycle logging."""
        msg      = ev.msg
        datapath = msg.datapath
        ofp      = datapath.ofproto

        reason_map = {
            ofp.OFPRR_IDLE_TIMEOUT  : "IDLE_TIMEOUT",
            ofp.OFPRR_HARD_TIMEOUT  : "HARD_TIMEOUT",
            ofp.OFPRR_DELETE        : "DELETE",
            ofp.OFPRR_GROUP_DELETE  : "GROUP_DELETE",
        }
        reason   = reason_map.get(msg.reason, f"UNKNOWN({msg.reason})")
        duration = msg.duration_sec + msg.duration_nsec / 1e9
        priority = msg.priority
        cookie   = msg.cookie
        n_pkts   = msg.packet_count
        n_bytes  = msg.byte_count

        LOG.info("[FLOW REMOVED] dpid=%s  reason=%-14s  duration=%.1fs  "
                 "priority=%d  pkts=%d  bytes=%d",
                 datapath.id, reason, duration, priority, n_pkts, n_bytes)

        # Update matching lifecycle record
        match_desc = str(dict(msg.match))
        for rec in reversed(self.flow_records):
            if rec.dpid == datapath.id and rec.removed is None:
                rec.mark_removed(reason, duration)
                break

        # Persist record snapshot to disk for regression checks
        self._dump_records()

    # ── Background monitor ────────────────────────────────────────────────────

    def _monitor_loop(self):
        while True:
            hub.sleep(15)
            total  = len(self.flow_records)
            active = sum(1 for r in self.flow_records if r.removed is None)
            removed = total - active
            LOG.info("[MONITOR] Total flow records: %d  active: %d  removed: %d",
                     total, active, removed)
            if removed:
                reasons = {}
                for r in self.flow_records:
                    if r.reason:
                        reasons[r.reason] = reasons.get(r.reason, 0) + 1
                LOG.info("[MONITOR] Removal reasons: %s", reasons)

    def _dump_records(self):
        path = "/tmp/flow_records.json"
        try:
            with open(path, "w") as f:
                json.dump([r.to_dict() for r in self.flow_records], f, indent=2)
        except Exception as e:
            LOG.error("Could not dump records: %s", e)
