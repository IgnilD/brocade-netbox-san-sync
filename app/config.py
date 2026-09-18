"""
Configuration loading.

Everything can come from config.yaml; secrets (passwords, tokens) can
also come from environment variables so you never have to bake
credentials into the image or the mounted file in Docker. Env vars win
over the YAML file when both are set. Pattern: `${VAR_NAME}` in the
YAML is substituted from the environment at load time; `${VAR_NAME:-default}`
falls back to `default` if unset.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-(.*?))?\}")


def _substitute_env(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var, _, default = m.groups()
            return os.environ.get(var, default if default is not None else "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


@dataclass
class SwitchConfig:
    name: str                              # friendly name, used in logs and as the NetBox Device name
    host: str
    username: str
    password: str
    method: Literal["ssh", "rest"] = "ssh"
    # optional plain NetBox Tag name applied to the switch's Device and
    # its Interfaces, purely for grouping/filtering in the UI (e.g. to
    # see "everything in Fabric-Test-A" at a glance). Just a Tag --
    # nothing plugin-specific, works on any NetBox install.
    fabric_tag: Optional[str] = None
    site: Optional[str] = None             # NetBox dcim.Site name/slug for the switch Device
    device_role: str = "SAN Switch"        # NetBox dcim.DeviceRole name
    timeout: int = 20

    ssh_port: int = 22
    ssh_key_path: Optional[str] = None
    ssh_new_session_per_command: bool = False

    rest_port: int = 443
    rest_verify_tls: bool = False


@dataclass
class NetBoxConfig:
    url: str = ""
    token: str = ""
    # true = verify against system CAs (fails on a self-signed cert);
    # false = skip verification (fine for internal instances, but
    # every request logs an insecure-request warning -- this tool
    # suppresses that warning automatically when you set false);
    # or a string path to a CA bundle file to verify against your own
    # internal CA properly instead of disabling verification at all.
    verify_tls: bool | str = True
    # tag applied to every object this tool creates/touches, so you can
    # always find (and, if needed, bulk-delete) everything it owns
    managed_tag: str = "brocade-sync"


@dataclass
class SyncOptions:
    dry_run: bool = False
    create_interfaces: bool = True
    create_cables: bool = True
    # what to do when a switch F-port has multiple logged-in WWNs (NPIV)
    # and none of them can be disambiguated as "the physical one" from
    # NetBox's own interface parent/child data: fall back to the first
    # WWN in the order the switch itself reported them.
    ambiguous_wwn_strategy: Literal["first", "skip"] = "first"
    # only touch interfaces belonging to switches, never delete anything
    # that wasn't created by this tool (identified via managed_tag)
    prune_missing_interfaces: bool = False


@dataclass
class AppConfig:
    netbox: NetBoxConfig
    switches: list[SwitchConfig] = field(default_factory=list)
    sync: SyncOptions = field(default_factory=SyncOptions)
    log_level: str = "INFO"


def load_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    raw = _substitute_env(raw)

    nb_raw = raw.get("netbox", {})
    netbox = NetBoxConfig(
        url=nb_raw.get("url", ""),
        token=nb_raw.get("token", ""),
        verify_tls=nb_raw.get("verify_tls", True),
        managed_tag=nb_raw.get("managed_tag", "brocade-sync"),
    )

    switches = []
    for sw in raw.get("switches", []):
        switches.append(
            SwitchConfig(
                name=sw["name"],
                host=sw["host"],
                username=sw["username"],
                password=sw["password"],
                method=sw.get("method", "ssh"),
                fabric_tag=sw.get("fabric") or sw.get("fabric_tag"),
                site=sw.get("site"),
                device_role=sw.get("device_role", "SAN Switch"),
                timeout=sw.get("timeout", 20),
                ssh_port=sw.get("ssh_port", 22),
                ssh_key_path=sw.get("ssh_key_path"),
                ssh_new_session_per_command=sw.get("ssh_new_session_per_command", False),
                rest_port=sw.get("rest_port", 443),
                rest_verify_tls=sw.get("rest_verify_tls", False),
            )
        )

    sync_raw = raw.get("sync", {})
    sync = SyncOptions(
        dry_run=sync_raw.get("dry_run", False),
        create_interfaces=sync_raw.get("create_interfaces", True),
        create_cables=sync_raw.get("create_cables", True),
        ambiguous_wwn_strategy=sync_raw.get("ambiguous_wwn_strategy", "first"),
        prune_missing_interfaces=sync_raw.get("prune_missing_interfaces", False),
    )

    return AppConfig(
        netbox=netbox,
        switches=switches,
        sync=sync,
        log_level=raw.get("log_level", "INFO"),
    )
