"""Builds the right BrocadeClient for a switch based on its config entry."""
from __future__ import annotations

from app.brocade.base import BrocadeClient
from app.brocade.rest_client import BrocadeRESTClient
from app.brocade.ssh_client import BrocadeSSHClient
from app.config import SwitchConfig


def build_client(switch: SwitchConfig) -> BrocadeClient:
    if switch.method == "ssh":
        return BrocadeSSHClient(
            host=switch.host,
            username=switch.username,
            password=switch.password,
            port=switch.ssh_port,
            timeout=switch.timeout,
            new_session_per_command=switch.ssh_new_session_per_command,
            ssh_key_path=switch.ssh_key_path,
        )
    elif switch.method == "rest":
        return BrocadeRESTClient(
            host=switch.host,
            username=switch.username,
            password=switch.password,
            verify_tls=switch.rest_verify_tls,
            timeout=switch.timeout,
            port=switch.rest_port,
            use_tls=switch.rest_use_tls,
        )
    raise ValueError(f"Unknown method '{switch.method}' for switch '{switch.name}' (use 'ssh' or 'rest')")
