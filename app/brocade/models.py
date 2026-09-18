"""
Normalized, backend-agnostic data models for everything pulled off a
Brocade switch.

Both the SSH backend (screen-scraping switchshow / portshow / nsshow /
sfpshow) and the REST backend (FOS REST API, FOS 8.2.1+) populate these
same dataclasses. Everything downstream (the NetBox sync code) only ever
talks to these models, never to raw SSH text or raw REST JSON. This is
what lets you point the sync at an old 7.4.2c switch via SSH or a new
9.1.1b switch via REST and get identical NetBox results.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PortState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    NO_LIGHT = "no_light"
    NO_SYNC = "no_sync"
    IN_SYNC = "in_sync"
    LASER_FLT = "laser_fault"
    DISABLED = "disabled"
    FAULTY = "faulty"
    UNKNOWN = "unknown"


class PortType(str, Enum):
    """Brocade port role, as reported by switchshow's 'Proto'/type column."""
    F_PORT = "F-Port"      # fabric port, device attached (initiator/target)
    FL_PORT = "FL-Port"    # fabric loop port
    E_PORT = "E-Port"      # inter-switch link
    EX_PORT = "EX-Port"    # FCR inter-fabric link
    N_PORT = "N-Port"      # NPIV proxy / AG uplink
    U_PORT = "U-Port"      # unknown / not yet online
    G_PORT = "G-Port"      # generic, not yet negotiated
    D_PORT = "D-Port"      # diagnostic port
    UNKNOWN = "unknown"


@dataclass
class SfpInfo:
    """Transceiver info, from sfpshow / media-rdp."""
    vendor_name: Optional[str] = None
    vendor_pn: Optional[str] = None
    vendor_sn: Optional[str] = None
    vendor_rev: Optional[str] = None
    connector: Optional[str] = None
    wavelength_nm: Optional[int] = None
    max_speed_gbps: Optional[float] = None
    temperature_c: Optional[float] = None
    rx_power_dbm: Optional[float] = None
    tx_power_dbm: Optional[float] = None


@dataclass
class NameServerEntry:
    """
    One row of the fabric name server (nsshow -t / fcnsshow / REST
    fibrechannel-name-server). This is the authoritative source for
    "what WWN is logged in, and through which local switch port".

    Two independent ways to tie an entry back to a local switch port --
    used in that preference order, since `port_index` matches
    switchshow's own Index column directly and needs no WWN parsing at
    all, while `fabric_port_name` depends on switchshow's F-port WWN
    column which some FOS builds only populate when exactly one device
    is logged in (see connection_sync.py for the full story):

        port_index        -- local switch port index (nsshow "Port Index")
        fabric_port_name  -- WWN of the local switch port ("Fabric Port Name")

    NPIV resolution: Brocade's name server tells you directly which
    entries are virtual and what physical WWN they belong to -- no
    NetBox-side guessing required. `device_type` is the raw string
    Brocade reports (e.g. "Physical Initiator+Target", "NPIV Target",
    "Physical Initiator"). For an NPIV entry, `permanent_port_name` is
    the WWN of the physical port it rides on (for a physical entry,
    it's simply equal to its own port_name).
    """
    port_id: str                             # FC address, e.g. "060200"
    port_name: str                           # WWPN of the device (physical or NPIV virtual)
    node_name: Optional[str]                 # WWNN of the device
    fabric_port_name: Optional[str] = None   # WWN of the local switch port it rides on (not always reliable, see above)
    permanent_port_name: Optional[str] = None  # for NPIV entries: the physical WWN this rides on
    port_index: Optional[int] = None         # local switch port index -- primary join key to switchshow
    device_type: Optional[str] = None        # raw Brocade string, e.g. "Physical Initiator+Target", "NPIV Target"
    symbolic_name: Optional[str] = None
    share_area: Optional[bool] = None

    @property
    def is_reported_npiv(self) -> bool:
        if self.device_type:
            return "npiv" in self.device_type.lower()
        return False

    @property
    def resolved_physical_wwn(self) -> Optional[str]:
        """The WWN to actually treat as 'the physical device on this
        port': for an NPIV entry, that's permanent_port_name (Brocade
        points it at the real HBA/target port for you); for a physical
        entry, it's just its own port_name."""
        if self.is_reported_npiv and self.permanent_port_name:
            return self.permanent_port_name
        return self.port_name


@dataclass
class PortInfo:
    """One physical port on the switch chassis."""
    index: int                         # 0-based port index (switchshow "Index") -- primary join key to the name server
    slot: int = 0                      # 1 for fixed-port switches
    port_number: Optional[int] = None  # "Port" column, may differ from index on bladed chassis
    name: str = ""                     # normalised interface name, e.g. "port0" or "slot1/port2"
    address: Optional[str] = None      # 24-bit fibre channel address
    wwn: Optional[str] = None          # switchshow's trailing WWN column, ONLY reliable when exactly
                                        # one device is logged in -- see connection_sync.py. Never
                                        # treat this as authoritative; it's best-effort/informational.
    state: PortState = PortState.UNKNOWN
    port_type: PortType = PortType.UNKNOWN
    enabled: bool = True
    speed_gbps: Optional[float] = None       # currently negotiated speed
    max_speed_gbps: Optional[float] = None   # port hardware capability
    npiv_enabled: bool = False               # port-level NPIV admin setting
    long_distance: bool = False
    trunked: bool = False
    description: Optional[str] = None        # user-set port name/alias on the switch, if available
    # Free-text tail switchshow prints after the port type for F-Ports,
    # e.g. "1 N Port + 1 NPIV public" when multiple devices are logged
    # in, or the lone attached WWN when only one is. Purely informational
    # -- matching/cabling never depends on this, see connection_sync.py.
    attachment_note: Optional[str] = None
    sfp: Optional[SfpInfo] = None


@dataclass
class SwitchInfo:
    """Chassis-level identity of the switch itself."""
    name: str
    wwn: str                          # switch WWN (fabric principal identity)
    domain_id: Optional[int] = None
    fabric_name: Optional[str] = None
    model: Optional[str] = None       # friendly product name, e.g. "Brocade 6510", "Brocade G620"
    switch_type: Optional[str] = None # raw switchshow switchType, e.g. "66.1" -- model is resolved from this
    part_number: Optional[str] = None # raw chassisshow "Part Num" -- fallback if switch_type isn't recognized
    serial_number: Optional[str] = None
    firmware: Optional[str] = None    # FOS version string, e.g. "7.4.2c"
    vendor: str = "Brocade"
    ip_address: Optional[str] = None       # host used to connect (SSH/REST target)
    mgmt_ip: Optional[str] = None          # switch's own reported management IP (ipaddrshow), may differ from ip_address
    mgmt_prefix_len: Optional[int] = None  # CIDR prefix length derived from ipaddrshow's subnet mask


@dataclass
class SwitchSnapshot:
    """Everything pulled from one switch in one sync pass."""
    switch: SwitchInfo
    ports: list[PortInfo] = field(default_factory=list)
    name_server: list[NameServerEntry] = field(default_factory=list)

    def ports_by_wwn(self) -> dict[str, PortInfo]:
        return {p.wwn: p for p in self.ports if p.wwn}

    def name_server_by_port_index(self) -> dict[int, list[NameServerEntry]]:
        """Group name-server entries by local switch port index -- the
        primary, reliable join key back to switchshow's Index column.
        Works regardless of NPIV, and doesn't depend on switchshow
        having printed a usable WWN in its port table."""
        buckets: dict[int, list[NameServerEntry]] = {}
        for entry in self.name_server:
            if entry.port_index is None:
                continue
            buckets.setdefault(entry.port_index, []).append(entry)
        return buckets

    def name_server_by_fabric_port_name(self) -> dict[str, list[NameServerEntry]]:
        """Secondary/fallback grouping, by the local switch port's own
        WWN as the name server itself reports it (nsshow's "Fabric Port
        Name" field -- distinct from, and more reliable than,
        switchshow's own WWN column). Used only when a name-server entry
        has no port_index for some reason."""
        buckets: dict[str, list[NameServerEntry]] = {}
        for entry in self.name_server:
            if not entry.fabric_port_name:
                continue
            buckets.setdefault(entry.fabric_port_name, []).append(entry)
        return buckets
