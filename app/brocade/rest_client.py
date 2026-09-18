"""
REST backend for Brocade FOS switches.

Only usable on FOS 8.2.1 and later (Brocade added the REST API then;
your 7.4.2c test switch does NOT have it -- use the SSH backend for
that one. Your 9.1.1b production switch supports this fully).

Uses the standard FOS REST session flow:
    POST /rest/login          (HTTP Basic auth) -> Authorization header
    GET  /rest/running/...    (with that Authorization header)
    POST /rest/logout

Resource module names below (brocade-fibrechannel-switch,
brocade-interface, brocade-name-server, brocade-media, brocade-chassis)
are FOS's standard YANG module names and have been stable since 8.2.1,
but FOS does add/rename leaf fields between minor releases occasionally.
If a field comes back missing on your switch, GET the resource once with
`--dump-raw` (see main.py) and check the exact field names your FOS
build uses -- most are unchanged for years, but it's worth a sanity
check on first run against a new switch.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests
import urllib3

from app.brocade.base import BrocadeClient, BrocadeClientError
from app.brocade.models import (
    NameServerEntry,
    PortInfo,
    PortState,
    PortType,
    SfpInfo,
    SwitchInfo,
    SwitchSnapshot,
)

log = logging.getLogger(__name__)

_STATE_MAP = {
    "online": PortState.ONLINE,
    "offline": PortState.OFFLINE,
    "no_light": PortState.NO_LIGHT,
    "no_module": PortState.OFFLINE,
    "no_sync": PortState.NO_SYNC,
    "in_sync": PortState.IN_SYNC,
    "laser_fault": PortState.LASER_FLT,
    "disabled": PortState.DISABLED,
    "faulty": PortState.FAULTY,
}

# FOS REST reports port-type as an integer enum in some releases and a
# string in others; this covers the common string form. Adjust if your
# switch returns integers (0=unknown,7=E-Port,15=F-Port,16=FL-Port, etc
# per the brocade-interface yang model) -- add an int branch in
# _map_port_type if you hit that on your build.
_PORTTYPE_MAP = {
    "f-port": PortType.F_PORT,
    "fl-port": PortType.FL_PORT,
    "e-port": PortType.E_PORT,
    "ex-port": PortType.EX_PORT,
    "n-port": PortType.N_PORT,
    "u-port": PortType.U_PORT,
    "g-port": PortType.G_PORT,
    "d-port": PortType.D_PORT,
}


def _map_port_type(raw: Any) -> PortType:
    if raw is None:
        return PortType.UNKNOWN
    return _PORTTYPE_MAP.get(str(raw).strip().lower(), PortType.UNKNOWN)


class BrocadeRESTClient(BrocadeClient):
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: int = 20,
        port: int = 443,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.port = port
        self.base_url = f"https://{host}:{port}/rest"
        self._session = requests.Session()
        self._auth_token: Optional[str] = None

        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # -- connection handling -------------------------------------------------

    def connect(self) -> None:
        try:
            resp = self._session.post(
                f"{self.base_url}/login",
                auth=(self.username, self.password),
                verify=self.verify_tls,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise BrocadeClientError(f"REST login to {self.host} failed: {exc}") from exc

        token = resp.headers.get("Authorization")
        if not token:
            raise BrocadeClientError(
                f"REST login to {self.host} returned no Authorization token "
                f"(status {resp.status_code}) -- check FOS version supports REST (8.2.1+)"
            )
        self._auth_token = token
        self._session.headers.update(
            {"Authorization": token, "Accept": "application/yang-data+json"}
        )

    def close(self) -> None:
        if self._auth_token:
            try:
                self._session.post(
                    f"{self.base_url}/logout", verify=self.verify_tls, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.warning("REST logout to %s failed (non-fatal): %s", self.host, exc)
        self._session.close()

    def _get(self, resource: str) -> dict:
        url = f"{self.base_url}/running/{resource}"
        resp = self._session.get(url, verify=self.verify_tls, timeout=self.timeout)
        if resp.status_code == 404:
            log.warning("REST resource not found on %s: %s", self.host, resource)
            return {}
        resp.raise_for_status()
        return resp.json()

    def dump_raw(self, resource: str) -> dict:
        """Exposed for `main.py --dump-raw`, to inspect exact JSON shape
        for tuning the field extraction below against your FOS build."""
        return self._get(resource)

    # -- top level ---------------------------------------------------------

    def get_snapshot(self) -> SwitchSnapshot:
        switch_info = self._get_switch_info()
        ports = self._get_ports()
        self._apply_sfp_info(ports)
        name_server = self._get_name_server()
        return SwitchSnapshot(switch=switch_info, ports=ports, name_server=name_server)

    # -- resource fetchers ---------------------------------------------------

    def _get_switch_info(self) -> SwitchInfo:
        data = self._get("brocade-fibrechannel-switch/fibrechannel-switch")
        rows = data.get("fibrechannel-switch", [])
        row = rows[0] if rows else {}

        chassis = self._get("brocade-chassis/chassis")
        model = chassis.get("chassis", {}).get("product-name")
        serial = chassis.get("chassis", {}).get("serial-number")

        domain = row.get("domain-id")
        return SwitchInfo(
            name=row.get("user-friendly-name") or row.get("name") or self.host,
            wwn=row.get("name", ""),  # brocade-fibrechannel-switch "name" leaf IS the switch WWN
            domain_id=int(domain) if domain is not None else None,
            fabric_name=row.get("fabric-user-friendly-name"),
            model=model,
            serial_number=serial,
            firmware=row.get("firmware-version"),
            ip_address=self.host,
        )

    def _get_ports(self) -> list[PortInfo]:
        data = self._get("brocade-interface/fibrechannel")
        rows = data.get("fibrechannel", [])
        ports: list[PortInfo] = []
        for row in rows:
            name = row.get("name", "")  # e.g. "0/3" (slot/port)
            slot, _, portnum = name.partition("/")
            try:
                index = int(row.get("index", portnum or 0))
            except (TypeError, ValueError):
                index = len(ports)

            speed_bps = row.get("speed")
            speed_gbps = float(speed_bps) / 1_000_000_000 if speed_bps else None

            ports.append(
                PortInfo(
                    index=index,
                    slot=int(slot) if slot.isdigit() else 0,
                    port_number=int(portnum) if portnum.isdigit() else None,
                    name=f"port{index}",
                    wwn=row.get("wwn"),
                    state=_STATE_MAP.get(str(row.get("physical-state", "")).lower(), PortState.UNKNOWN),
                    port_type=_map_port_type(row.get("port-type")),
                    enabled=row.get("enabled-state") == 1 or row.get("enabled-state") is True,
                    speed_gbps=speed_gbps,
                    max_speed_gbps=None,
                    npiv_enabled=bool(row.get("npiv-enabled")),
                    long_distance=bool(row.get("long-distance")),
                    trunked=bool(row.get("is-trunk-port") or row.get("trunk-port-role")),
                    description=row.get("user-friendly-name"),
                )
            )
        return ports

    def _apply_sfp_info(self, ports: list[PortInfo]) -> None:
        try:
            data = self._get("brocade-media/media-rdp")
        except requests.RequestException as exc:
            log.warning("brocade-media/media-rdp unavailable on %s: %s", self.host, exc)
            return

        rows = data.get("media-rdp", [])
        ports_by_name = {p.name: p for p in ports}
        # media-rdp entries are keyed by the same "name" (slot/port) as
        # brocade-interface/fibrechannel; re-derive our normalized name.
        ports_by_index = {p.index: p for p in ports}
        for row in rows:
            name = row.get("name", "")
            _, _, portnum = name.partition("/")
            idx = int(portnum) if portnum.isdigit() else None
            port = ports_by_index.get(idx) if idx is not None else ports_by_name.get(name)
            if port is None:
                continue
            sfp = SfpInfo(
                vendor_name=row.get("vendor-name"),
                vendor_pn=row.get("vendor-part-number"),
                vendor_sn=row.get("vendor-serial-number"),
                vendor_rev=row.get("vendor-revision"),
                connector=row.get("connector"),
                wavelength_nm=int(row["wavelength"]) if row.get("wavelength") else None,
                temperature_c=float(row["temperature"]) if row.get("temperature") else None,
                rx_power_dbm=float(row["rx-power"]) if row.get("rx-power") else None,
                tx_power_dbm=float(row["tx-power"]) if row.get("tx-power") else None,
            )
            port.sfp = sfp

    def _get_name_server(self) -> list[NameServerEntry]:
        data = self._get("brocade-name-server/fibrechannel-name-server")
        rows = data.get("fibrechannel-name-server", [])
        entries: list[NameServerEntry] = []
        for row in rows:
            entries.append(
                NameServerEntry(
                    port_id=str(row.get("port-id", "")),
                    port_name=row.get("port-name", ""),
                    node_name=row.get("node-name"),
                    # "physical-port-name" is FOS REST's field for what
                    # the SSH CLI calls "Fabric Port Name" -- the local
                    # switch port's own WWN this device logged in through.
                    fabric_port_name=row.get("physical-port-name"),
                    device_type="NPIV" if row.get("npiv") else None,
                    symbolic_name=row.get("port-symbolic-name"),
                    share_area=bool(row.get("share-area")) if "share-area" in row else None,
                )
            )
        return entries
