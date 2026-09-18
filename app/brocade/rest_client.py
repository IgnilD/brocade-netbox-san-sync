"""
REST backend for Brocade FOS switches.

Only usable on FOS 8.2.1 and later (Brocade added the REST API then;
your 7.4.2c test switch does NOT have it -- use the SSH backend for
that one. Your 9.1.1b production switch supports this fully).

FOS's REST API is normally HTTPS-only, so that's the default here
(`rest_use_tls: true`). If your switch is only reachable through
something that terminates TLS elsewhere and forwards plain HTTP
(a reverse proxy, jump host, etc. -- not a native FOS behavior, but a
real thing some network setups do), set `rest_use_tls: false` in that
switch's config; forcing HTTPS against a plain-HTTP listener fails with
an SSL "record layer failure", not a helpful error.

Uses the standard FOS REST session flow:
    POST /rest/login          (HTTP Basic auth) -> Authorization header
    GET  /rest/running/...    (with that Authorization header)
    POST /rest/logout

Resource paths and field names below were verified against Broadcom's
own published FOS 9.2.x REST API Reference Manual
(techdocs.broadcom.com/.../fabric-os-rest-api/9-2-x/...), not guessed --
an earlier version of this file guessed several field names wrong (most
importantly, missed that every GET response is wrapped in a top-level
"Response" object, which alone made every field extraction silently
return nothing). If a field still comes back missing on your switch,
GET the resource once with `--dump-raw` (see main.py) and compare
against that manual -- FOS does rename/deprecate leaf fields between
releases (the interface fields below already carry old-name fallbacks
for exactly this reason).
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


def _first_present(row: dict, *keys: str):
    """Returns the value of the first key present in `row`, or None.
    Used throughout below because FOS REST has renamed several leaf
    fields across versions (e.g. `enabled-state` -> `is-enabled-state`)
    -- trying the current name first, then falling back to older names,
    covers more FOS versions than hardcoding one."""
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _parse_protocol_speed(raw: Optional[str]) -> Optional[float]:
    """protocol-speed is a string like "16-gfc" or "32-gfc" -- extract
    the leading number as Gbps."""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw).split("-")[0] if ch.isdigit())
    return float(digits) if digits else None


class BrocadeRESTClient(BrocadeClient):
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout: int = 20,
        port: int = 443,
        use_tls: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.port = port
        self.use_tls = use_tls
        scheme = "https" if use_tls else "http"
        self.base_url = f"{scheme}://{host}:{port}/rest"
        self._session = requests.Session()
        self._auth_token: Optional[str] = None

        if port == 80 and use_tls:
            log.warning(
                "REST config for %s uses port 80 with TLS still enabled (rest_use_tls defaults to "
                "true) -- port 80 is conventionally plain HTTP, and this combination will likely "
                "fail with an SSL 'record layer failure'. If this switch is only reachable over "
                "plain HTTP, set `rest_use_tls: false` for it.",
                host,
            )

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
        """GETs one REST resource and returns its content, already
        unwrapped from FOS's top-level "Response" envelope -- EVERY FOS
        REST GET response is wrapped like
        `{"Response": {"<resource-name>": ...}}`, confirmed against
        Broadcom's own documented examples. Missing this wrapper was the
        single biggest bug in an earlier version of this client: every
        field lookup silently found nothing, because it was looking one
        level too shallow.
        """
        url = f"{self.base_url}/running/{resource}"
        resp = self._session.get(url, verify=self.verify_tls, timeout=self.timeout)
        if resp.status_code == 404:
            log.warning("REST resource not found on %s: %s", self.host, resource)
            return {}
        resp.raise_for_status()
        data = resp.json()
        return data.get("Response", data)

    def dump_raw(self, resource: str) -> dict:
        """Exposed for `main.py --dump-raw`, to inspect exact JSON shape
        for tuning the field extraction below against your FOS build.
        Returns the raw response including the "Response" wrapper, so
        what you see here matches Broadcom's own documented examples
        exactly (unlike the already-unwrapped data `_get` returns
        internally)."""
        url = f"{self.base_url}/running/{resource}"
        resp = self._session.get(url, verify=self.verify_tls, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

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

        chassis_data = self._get("brocade-chassis/chassis")
        chassis = chassis_data.get("chassis", {})

        domain = row.get("domain-id")
        return SwitchInfo(
            name=row.get("user-friendly-name") or row.get("name") or self.host,
            wwn=row.get("name", ""),  # brocade-fibrechannel-switch "name" leaf IS the switch WWN
            domain_id=int(domain) if domain is not None else None,
            fabric_name=row.get("fabric-user-friendly-name"),
            model=row.get("model") or chassis.get("product-name"),
            part_number=chassis.get("part-number"),
            serial_number=chassis.get("serial-number"),
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

            speed_gbps = _parse_protocol_speed(row.get("protocol-speed"))

            enabled_raw = _first_present(row, "is-enabled-state", "enabled-state")
            port_type_raw = _first_present(row, "port-type-string", "port-type")
            npiv_raw = _first_present(row, "npiv-enabled-v2", "npiv-enabled")
            trunk_raw = _first_present(row, "trunk-port-enabled-v2", "trunk-port-enabled", "is-trunk-port")
            long_dist_raw = _first_present(row, "long-distance-string", "long-distance")

            ports.append(
                PortInfo(
                    index=index,
                    slot=int(slot) if slot.isdigit() else 0,
                    port_number=int(portnum) if portnum.isdigit() else None,
                    name=f"port{index}",
                    wwn=row.get("wwn"),
                    state=_STATE_MAP.get(str(row.get("physical-state", "")).lower(), PortState.UNKNOWN),
                    port_type=_map_port_type(port_type_raw),
                    enabled=bool(enabled_raw) if enabled_raw is not None else True,
                    speed_gbps=speed_gbps,
                    max_speed_gbps=None,
                    npiv_enabled=bool(npiv_raw),
                    long_distance=bool(long_dist_raw),
                    trunked=bool(trunk_raw),
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
            port_index = row.get("port-index")
            entries.append(
                NameServerEntry(
                    port_id=str(row.get("port-id", "")),
                    port_name=row.get("port-name", ""),
                    node_name=row.get("node-name"),
                    # the local switch port's own WWN this device logged
                    # in through -- "fabric-port-name", confirmed against
                    # Broadcom's documented example response (an earlier
                    # version of this file used the wrong field name here)
                    fabric_port_name=row.get("fabric-port-name"),
                    # for an NPIV entry, the physical WWN it rides on
                    permanent_port_name=row.get("permanent-port-name"),
                    port_index=int(port_index) if port_index is not None else None,
                    # raw string like "Physical Initiator", "NPIV Target"
                    # -- same convention as the SSH CLI's "Device type:",
                    # not a boolean (an earlier version of this file
                    # looked for a nonexistent "npiv" boolean field)
                    device_type=row.get("name-server-device-type"),
                    symbolic_name=row.get("port-symbolic-name"),
                )
            )
        return entries
