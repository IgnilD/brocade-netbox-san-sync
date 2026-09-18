from __future__ import annotations

import logging

from app.brocade.models import SwitchSnapshot
from app.config import AppConfig, SwitchConfig
from app.netbox.client import NetBoxSyncClient
from app.utils.wwn import normalize_wwn

log = logging.getLogger(__name__)


def sync_interfaces(nb: NetBoxSyncClient, config: AppConfig, switch_cfg: SwitchConfig, device, snapshot: SwitchSnapshot) -> dict[int, object]:
    """Creates/updates one dcim.Interface per physical switch port
    (name, own WWN -- core NetBox's built-in Interface.wwn field,
    negotiated speed/type, enabled state, description). Returns a map of
    {port.index: netbox interface object} for the connection-sync step
    to use.

    NPIV note: virtual WWNs live on the far-end HOST's HBA, never on the
    switch's own port -- a Brocade F-port is always physical. So no
    virtual/child interfaces are created here; that only happens (if at
    all) on the device side, which is out of this tool's scope by
    design (see README: "verify, don't invent, the far end").
    """
    if not config.sync.create_interfaces or device is None:
        log.info("interface sync disabled or device unresolved (dry-run) -- skipping")
        return {}

    interfaces: dict[int, object] = {}
    for port in snapshot.ports:
        port.wwn = normalize_wwn(port.wwn)
        iface = nb.get_or_create_port_interface(device, port, switch_cfg)
        if iface is not None:
            interfaces[port.index] = iface
    log.info("switch '%s': synced %d interfaces", device.name if device else "?", len(interfaces))
    return interfaces
