from __future__ import annotations

import logging

from app.brocade.base import BrocadeClientError
from app.brocade.factory import build_client
from app.config import AppConfig
from app.netbox.client import NetBoxSyncClient
from app.sync.connection_sync import sync_connections
from app.sync.interface_sync import sync_interfaces
from app.sync.switch_sync import sync_switch

log = logging.getLogger(__name__)


def run_sync(config: AppConfig) -> int:
    """Runs one full sync pass over every configured switch. Returns the
    number of switches that failed (0 = clean run), so main.py can set
    a non-zero exit code for cron/CI use without crashing mid-run on one
    bad switch."""
    nb = NetBoxSyncClient(config)
    failures = 0

    for switch_cfg in config.switches:
        log.info("=== syncing switch '%s' (%s via %s) ===", switch_cfg.name, switch_cfg.host, switch_cfg.method)
        try:
            client = build_client(switch_cfg)
            with client:
                snapshot = client.get_snapshot()
        except BrocadeClientError as exc:
            log.error("switch '%s' failed: %s", switch_cfg.name, exc)
            failures += 1
            continue
        except Exception:
            log.exception("switch '%s' failed with an unexpected error", switch_cfg.name)
            failures += 1
            continue

        try:
            device = sync_switch(nb, switch_cfg, snapshot)
            interfaces = sync_interfaces(nb, config, switch_cfg, device, snapshot)
            sync_connections(nb, config, snapshot, interfaces)
        except Exception:
            log.exception("switch '%s': NetBox sync step failed", switch_cfg.name)
            failures += 1

    return failures
