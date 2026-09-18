"""
SSH backend for Brocade FOS switches.

Works on every FOS version (there's no REST API before 8.2.1, so this is
the *only* option for old switches -- e.g. the 7.4.2c switch in the
prompt this was built for -- and remains a valid choice on new firmware
too). Screen-scrapes well-known CLI commands:

    switchshow    -> switch identity + bulk port list (state, speed,
                      port type; NOT a reliable per-port WWN -- see below)
    portshow <N>  -> each present port's OWN WWN ("portWwn:"), fetched
                      per-port since switchshow's trailing column is
                      actually the ATTACHED DEVICE's info, not the
                      switch's own -- see _apply_own_port_wwns()
    sfpshow -all  -> per-port transceiver details
    nsshow -t     -> full fabric name server (which device WWNs are
                      logged in, and through which local port)
    version       -> firmware version
    chassisshow   -> chassis model / serial number

The exact text layout of these commands has drifted a little release to
release. The regexes below are built from the well-documented, stable
FOS output format, but if your switch's output doesn't match, run with
`--dump-raw <command>` (see main.py) to capture the exact text and
adjust the regex here -- that's expected tuning, not a bug.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import paramiko

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
from app.brocade.switch_type_lookup import model_for_switch_type
from app.utils.network import netmask_to_prefixlen

log = logging.getLogger(__name__)

WWN_PATTERN = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){7}"

_STATE_MAP = {
    "online": PortState.ONLINE,
    "offline": PortState.OFFLINE,
    "no_light": PortState.NO_LIGHT,
    "no_module": PortState.OFFLINE,
    "no_sync": PortState.NO_SYNC,
    "in_sync": PortState.IN_SYNC,
    "laser_flt": PortState.LASER_FLT,
    "disabled": PortState.DISABLED,
    "testing": PortState.UNKNOWN,
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

# switchshow port table row, e.g.:
#   3    3   030300   id    N8    Online      FC  F-Port  50:06:01:60:47:20:12:34
#   2    2   060200   id    N8    Online      FC  F-Port  1 N Port + 1 NPIV public
# The text after the port type is NOT reliably a WWN -- it's a WWN only
# when exactly one device is logged in on that port; with NPIV logins it
# becomes a free-text summary instead ("1 N Port + 1 NPIV public"). So
# we capture it generically as `trailing` and only promote it to `wwn`
# if it happens to fully match a WWN -- see _parse_switchshow. Matching
# device WWNs to ports authoritatively is done from `nsshow -t`'s own
# "Port Index" field instead (see connection_sync.py), never from here.
_SWITCHSHOW_ROW_RE = re.compile(
    r"^\s*(?P<index>\d+)\s+"
    r"(?P<port>\d+)\s+"
    r"(?P<address>[0-9A-Fa-f]{6})\s+"
    r"(?P<media>\S+)\s+"
    r"(?P<speed>\S+)\s+"
    r"(?P<state>\S+)"
    r"(?:\s+(?P<proto>FC))?"
    r"(?:\s+(?P<porttype>[EFUNGD]L?X?-Port))?"
    r"(?:\s+(?P<trailing>\S.*?))?\s*$"
)

_SWITCHSHOW_KV_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9 ._]*?):\s*(?P<value>.*)$")


def _parse_speed(raw: Optional[str]) -> Optional[float]:
    """Normalizes '16G', 'N16', '8Gbps', 'AN' etc into Gbps as a float."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.upper() in ("AN", "--", "N/A", ""):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", raw)
    if not m:
        return None
    return float(m.group(1))


class BrocadeSSHClient(BrocadeClient):
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        timeout: int = 20,
        new_session_per_command: bool = False,
        ssh_key_path: Optional[str] = None,
        accept_unknown_host_keys: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.new_session_per_command = new_session_per_command
        self.ssh_key_path = ssh_key_path
        self.accept_unknown_host_keys = accept_unknown_host_keys
        self._client: Optional[paramiko.SSHClient] = None

    # -- connection handling -------------------------------------------------

    def connect(self) -> None:
        self._client = paramiko.SSHClient()
        if self.accept_unknown_host_keys:
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            self._client.load_system_host_keys()
        try:
            self._client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                key_filename=self.ssh_key_path,
                timeout=self.timeout,
                allow_agent=False,
                look_for_keys=bool(self.ssh_key_path),
            )
        except Exception as exc:  # paramiko raises several distinct types
            raise BrocadeClientError(f"SSH connect to {self.host} failed: {exc}") from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _run(self, command: str) -> str:
        """Runs one CLI command and returns its raw stdout.

        Uses exec_command (no pty), which is the standard way to script
        Brocade FOS over SSH -- it avoids the '---More---' pager that
        an interactive shell session would trigger on long output. Some
        very old firmware only tolerates one exec channel per TCP
        connection; set `new_session_per_command: true` in config if you
        see connection resets on the second command.
        """
        if self._client is None:
            raise BrocadeClientError("not connected")

        if self.new_session_per_command:
            self.close()
            self.connect()

        assert self._client is not None
        stdin, stdout, stderr = self._client.exec_command(command, timeout=self.timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if err.strip():
            log.debug("stderr from '%s' on %s: %s", command, self.host, err.strip())
        return out

    def dump_raw(self, command: str) -> str:
        """Exposed for `main.py --dump-raw`, to inspect exact output for
        tuning the regexes above against a specific FOS build."""
        return self._run(command)

    # -- top level -------------------------------------------------------

    def get_snapshot(self) -> SwitchSnapshot:
        switchshow_out = self._run("switchshow")
        switch_info, ports = self._parse_switchshow(switchshow_out)

        try:
            version_out = self._run("version")
            switch_info.firmware = self._parse_firmware(version_out)
        except Exception as exc:
            log.warning("`version` failed/unsupported on %s: %s", self.host, exc)

        try:
            chassis_out = self._run("chassisshow")
            part_number, serial = self._parse_chassisshow(chassis_out)
            switch_info.part_number = part_number
            switch_info.serial_number = serial
        except Exception as exc:
            log.warning("`chassisshow` failed/unsupported on %s: %s", self.host, exc)

        # switchType (from switchshow) is the canonical source for the
        # product name -- chassisshow's raw part number is only a
        # fallback for switchTypes not in our lookup table (e.g. very
        # new or very obscure hardware).
        switch_info.model = (
            model_for_switch_type(switch_info.switch_type)
            or switch_info.part_number
            or "Unknown Brocade Switch"
        )

        try:
            ip_out = self._run("ipaddrshow")
            mgmt_ip, prefix_len = self._parse_ipaddrshow(ip_out)
            if mgmt_ip:
                switch_info.mgmt_ip = mgmt_ip
                switch_info.mgmt_prefix_len = prefix_len
        except Exception as exc:
            log.warning("`ipaddrshow` failed/unsupported on %s: %s", self.host, exc)

        try:
            sfp_out = self._run("sfpshow -all")
            self._apply_sfp_info(sfp_out, ports)
        except Exception as exc:
            log.warning("sfpshow -all failed/unsupported on %s: %s", self.host, exc)

        self._apply_own_port_wwns(ports)

        ns_out = self._run("nsshow -t")
        name_server = self._parse_nsshow(ns_out)

        return SwitchSnapshot(switch=switch_info, ports=ports, name_server=name_server)

    # -- parsers -----------------------------------------------------------

    def _parse_switchshow(self, text: str) -> tuple[SwitchInfo, list[PortInfo]]:
        kv: dict[str, str] = {}
        ports: list[PortInfo] = []
        in_table = False

        for line in text.splitlines():
            if line.strip().startswith("===="):
                in_table = True
                continue
            if not in_table:
                m = _SWITCHSHOW_KV_RE.match(line)
                if m:
                    kv[m.group("key").strip()] = m.group("value").strip()
                continue

            m = _SWITCHSHOW_ROW_RE.match(line)
            if not m:
                continue
            state_raw = m.group("state").lower()
            porttype_raw = (m.group("porttype") or "").lower()

            trailing = (m.group("trailing") or "").strip()
            # switchshow's trailing text is the ATTACHED DEVICE's info
            # (its WWN when exactly one device is logged in, or a
            # free-text NPIV summary otherwise) -- confirmed against a
            # real `portshow` on this exact port: the switch's own port
            # WWN and the device's WWN are different values, and this
            # trailing column is the device's. It must never be written
            # to PortInfo.wwn (the switch's own port WWN) -- doing so
            # previously caused devices to falsely "self-match" the
            # switch's own interface during cabling. It's kept only as
            # an informational note.
            attachment_note = trailing or None

            port = PortInfo(
                index=int(m.group("index")),
                port_number=int(m.group("port")),
                name=f"port{m.group('index')}",
                address=m.group("address"),
                wwn=None,  # populated afterward via portshow, see get_snapshot()
                attachment_note=attachment_note,
                state=_STATE_MAP.get(state_raw, PortState.UNKNOWN),
                port_type=_PORTTYPE_MAP.get(porttype_raw, PortType.UNKNOWN),
                speed_gbps=_parse_speed(m.group("speed")),
                enabled=state_raw != "disabled",
            )
            ports.append(port)

        wwn = kv.get("switchWwn", "")
        switch_info = SwitchInfo(
            name=kv.get("switchName", self.host),
            wwn=wwn,
            domain_id=int(kv["switchDomain"]) if kv.get("switchDomain", "").strip().isdigit() else None,
            fabric_name=kv.get("Fabric Name") or None,
            switch_type=kv.get("switchType") or None,
            ip_address=self.host,
        )
        return switch_info, ports

    @staticmethod
    def _parse_firmware(text: str) -> Optional[str]:
        # `version` output includes a line like: "Fabric OS:  v9.1.1b"
        m = re.search(r"Fabric OS:\s*v?([\w.\-]+)", text)
        return m.group(1) if m else None

    @staticmethod
    def _parse_chassisshow(text: str) -> tuple[Optional[str], Optional[str]]:
        # Looks for the chassis unit's "Part Num" / model line and the
        # chassis "Serial Num". chassisshow lists several FRUs; we want
        # the first "CHASSIS" block.
        model = None
        serial = None
        block = []
        capture = False
        for line in text.splitlines():
            if re.match(r"^\s*CHASSIS\b", line, re.IGNORECASE):
                capture = True
                continue
            if capture and line.strip() == "":
                break
            if capture:
                block.append(line)
        block_text = "\n".join(block)
        m = re.search(r"Part Num:\s*([\w\-]+)", block_text)
        if m:
            model = m.group(1)
        m = re.search(r"Serial Num:\s*([\w\-]+)", block_text)
        if m:
            serial = m.group(1)
        return model, serial

    @staticmethod
    def _parse_ipaddrshow(text: str) -> tuple[Optional[str], Optional[int]]:
        """Parses `ipaddrshow` for the switch's own management IP.

        On a director-class chassis this command prints separate CP0/CP1
        sections instead of one "SWITCH" section; this takes the first
        "Ethernet IP Address" it finds, which is correct for fixed-port
        switches (the common case) but on a director will report
        whichever CP happens to be listed first, not necessarily the
        active one. Good enough as a best-effort default; flag it if you
        need per-CP accuracy for director hardware.
        """
        ip_match = re.search(r"Ethernet IP Address:\s*(\d{1,3}(?:\.\d{1,3}){3})", text)
        mask_match = re.search(r"Ethernet Subnetmask:\s*(\d{1,3}(?:\.\d{1,3}){3})", text)
        if not ip_match:
            return None, None
        return ip_match.group(1), netmask_to_prefixlen(mask_match.group(1) if mask_match else None)

    @staticmethod
    def _apply_sfp_info(text: str, ports: list[PortInfo]) -> None:
        ports_by_index = {p.index: p for p in ports}
        blocks = re.split(r"\n(?=Port:\s*\d+)", text)
        for block in blocks:
            m = re.match(r"Port:\s*(\d+)", block.strip())
            if not m:
                continue
            idx = int(m.group(1))
            port = ports_by_index.get(idx)
            if port is None:
                continue

            def find(pattern: str) -> Optional[str]:
                mm = re.search(pattern, block)
                return mm.group(1).strip() if mm else None

            sfp = SfpInfo(
                vendor_name=find(r"Vendor Name\s*(.+)"),
                vendor_pn=find(r"Vendor PN\s*(.+)"),
                vendor_sn=find(r"Serial No\s*(.+)"),
                vendor_rev=find(r"Vendor Rev\s*(.+)"),
                connector=find(r"Connector\s*(.+)"),
            )
            wl = find(r"Wavelength\s*(\d+)")
            if wl:
                sfp.wavelength_nm = int(wl)
            rx = find(r"RX Power\s*([\-\d.]+)\s*dBm")
            if rx:
                sfp.rx_power_dbm = float(rx)
            tx = find(r"TX Power\s*([\-\d.]+)\s*dBm")
            if tx:
                sfp.tx_power_dbm = float(tx)
            port.sfp = sfp

    def _apply_own_port_wwns(self, ports: list[PortInfo]) -> None:
        """Fetches each present port's OWN WWN via `portshow <index>`,
        which reports it on a `portWwn:` line -- distinct from (and
        confirmed, against real switch output, to be a different value
        than) the `portWwn of device(s) connected:` section a few lines
        below it, which is the attached device's WWN instead. Only
        `portWwn:` (anchored so it can't match the "of device(s)" line)
        is used here. Only queried for ports that have a module present
        (state != OFFLINE) to avoid ~40 pointless SSH round trips per
        sync for empty slots; failures are per-port and non-fatal, since
        this is enrichment, not something cabling logic depends on
        (connection_sync joins on Port Index, not on this WWN).
        """
        own_wwn_re = re.compile(r"^portWwn:\s*(" + WWN_PATTERN + r")\s*$", re.MULTILINE)
        for port in ports:
            if port.state == PortState.OFFLINE:
                continue
            try:
                out = self._run(f"portshow {port.index}")
            except Exception as exc:
                log.debug("portshow %d failed on %s (non-fatal): %s", port.index, self.host, exc)
                continue
            m = own_wwn_re.search(out)
            if m:
                port.wwn = m.group(1)
            else:
                log.debug("portshow %d on %s had no parseable 'portWwn:' line", port.index, self.host)

    @staticmethod
    def _parse_nsshow(text: str) -> list[NameServerEntry]:
        """
        Parses `nsshow -t`. Entries are NOT reliably blank-line-separated
        across FOS builds (some print a blank line between entries, some
        don't) -- so instead of splitting on blank lines, we detect the
        start of a new entry directly: a line beginning with a short
        type code (N, NL, F, ...) followed by the 6-hex-digit FC address
        and a semicolon, e.g. " N    060200;      3;<wwn>;<wwn>; na".
        Everything up to the next such line belongs to that entry.
        """
        entry_start_re = re.compile(r"^\s*[A-Za-z]{1,3}\s+([0-9A-Fa-f]{6});")

        def build_entry(block_lines: list[str]) -> Optional[NameServerEntry]:
            if not block_lines:
                return None
            first_line = block_lines[0]
            pid_match = entry_start_re.match(first_line)
            # PortName/NodeName are on the entry's own first line; WWNs
            # appearing later (Fabric/Permanent Port Name) are extracted
            # separately below, so restrict this findall to the first
            # line only or we'd pick up the wrong ones.
            wwns = re.findall(WWN_PATTERN, first_line)
            if not pid_match or len(wwns) < 2:
                return None

            block = "\n".join(block_lines)
            fpn_match = re.search(r"Fabric Port Name:\s*(" + WWN_PATTERN + r")", block)
            ppn_match = re.search(r"Permanent Port Name:\s*(" + WWN_PATTERN + r")", block)
            idx_match = re.search(r"Port Index:\s*(\d+)", block)
            devtype_match = re.search(r"Device type:\s*(.+)", block)
            symb_match = re.search(r'(?:Port|Node)Symb:\s*\[\d+\]\s*"([^"]*)"', block)

            return NameServerEntry(
                port_id=pid_match.group(1),
                port_name=wwns[0],
                node_name=wwns[1],
                fabric_port_name=fpn_match.group(1) if fpn_match else None,
                permanent_port_name=ppn_match.group(1) if ppn_match else None,
                port_index=int(idx_match.group(1)) if idx_match else None,
                device_type=devtype_match.group(1).strip() if devtype_match else None,
                symbolic_name=symb_match.group(1) if symb_match else None,
            )

        entries: list[NameServerEntry] = []
        current: list[str] = []
        for line in text.splitlines():
            if entry_start_re.match(line):
                entry = build_entry(current)
                if entry:
                    entries.append(entry)
                current = [line]
            elif current:
                current.append(line)
        entry = build_entry(current)
        if entry:
            entries.append(entry)
        return entries
