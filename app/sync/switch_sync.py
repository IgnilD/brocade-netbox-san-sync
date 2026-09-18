from __future__ import annotations

import logging

from app.brocade.models import SwitchSnapshot
from app.config import SwitchConfig
from app.netbox.client import NetBoxSyncClient

log = logging.getLogger(__name__)


def sync_switch(nb: NetBoxSyncClient, switch_cfg: SwitchConfig, snapshot: SwitchSnapshot):
    """Ensures the chassis Device exists as a core dcim.Device (plus the
    optional fabric_tag, if configured), and its primary management IP
    if one was discovered. Returns the device (or None in dry-run mode)."""
    device = nb.get_or_create_switch_device(switch_cfg, snapshot.switch)
    log.info(
        "switch '%s': model=%s fw=%s serial=%s domain=%s mgmt_ip=%s",
        switch_cfg.name,
        snapshot.switch.model,
        snapshot.switch.firmware,
        snapshot.switch.serial_number,
        snapshot.switch.domain_id,
        snapshot.switch.mgmt_ip,
    )
    if device is not None and snapshot.switch.mgmt_ip:
        nb.assign_primary_ip(device, snapshot.switch.mgmt_ip, snapshot.switch.mgmt_prefix_len)
    return device
