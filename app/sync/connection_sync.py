from __future__ import annotations

import logging

from app.brocade.models import NameServerEntry, PortType, SwitchSnapshot
from app.config import AppConfig
from app.netbox.client import NetBoxSyncClient
from app.utils.wwn import normalize_wwn

log = logging.getLogger(__name__)

_DEVICE_FACING = {PortType.F_PORT, PortType.FL_PORT}


def _resolve_physical_interface(nb: NetBoxSyncClient, wwn: str):
    """Looks up a WWN in NetBox. If NetBox itself models it as a virtual
    interface (Interface.parent set -- e.g. someone onboarded a host and
    entered its NPIV WWPNs as children of the physical HBA interface),
    walks up to the physical parent, since a Cable can only terminate on
    a physical interface. This is an independent safety net on top of
    the Brocade-native resolution in sync_connections() below -- either
    one alone would catch most cases, both together catch more.
    Returns (interface_to_cable, was_virtual_in_netbox) or (None, False).
    """
    iface = nb.find_interface_by_wwn(wwn)
    if iface is None:
        return None, False

    parent = getattr(iface, "parent", None)
    if not parent:
        return iface, False

    parent_id = parent["id"] if isinstance(parent, dict) else parent.id
    physical = nb.nb.dcim.interfaces.get(parent_id)
    return physical, True


def _target_wwn_for_entry(entry: NameServerEntry) -> str | None:
    """The WWN to actually try to match/cable for one name-server entry.

    Brocade tells us directly, per entry, whether it's physical or NPIV
    (`Device type`), and for NPIV entries, exactly which physical WWN it
    rides on (`Permanent Port Name`) -- no guessing required. Only when
    that information is missing (older FOS / some REST builds don't
    report `Device type`) do we fall back to using the entry's own
    PortName and let the ambiguity handling below sort it out."""
    resolved = entry.resolved_physical_wwn  # Permanent Port Name for NPIV, else PortName
    return normalize_wwn(resolved)


def sync_connections(nb: NetBoxSyncClient, config: AppConfig, snapshot: SwitchSnapshot, interfaces: dict[int, object]) -> None:
    """For every device-facing port (F-Port/FL-Port), figures out which
    WWNs are logged in (via the name server, joined to the port by
    **Port Index** -- switchshow's own WWN column is not reliable enough
    to key off, see ssh_client.py), resolves each to the WWN of the
    *physical* device behind it (using Brocade's own `Device type` /
    `Permanent Port Name` fields when available, i.e. real NPIV
    resolution rather than a heuristic), looks that WWN up in NetBox,
    and creates a Cable to it if found.

    Every device-facing port with logged-in WWNs gets exactly one INFO
    (or higher) log line explaining what happened to it -- resolved and
    cabled, resolved but already cabled, WWN(s) not in NetBox at all,
    self-matched (a data problem, see ensure_cable), or ambiguous. That
    accounting is also cross-checked against the final tally at the end
    of the run, so a silently-dropped port would show up as a mismatch
    warning rather than just vanishing from the numbers.

    Ambiguity handling (config.sync.ambiguous_wwn_strategy) is only a
    last-resort fallback: it only fires when a port's logged-in entries
    genuinely disagree on which physical WWN they belong to (which
    shouldn't happen with well-formed Device type/Permanent Port Name
    data, but can on older firmware that doesn't report those fields).
    Default ("first") cables to the first-listed WWN in that case;
    "skip" leaves the port uncabled and logs a warning instead.
    """
    if not config.sync.create_cables:
        log.info("cable sync disabled -- skipping")
        return

    by_port_index = snapshot.name_server_by_port_index()
    by_fabric_port_name = snapshot.name_server_by_fabric_port_name()

    # tallies by what actually happened, not just "we tried" -- see
    # NetBoxSyncClient.ensure_cable() for what each status means. Every
    # device-facing port with entries lands in exactly one bucket here,
    # so these should always sum to "ports_with_entries" below.
    status_counts: dict[str, int] = {}
    ports_with_entries = 0

    for port in snapshot.ports:
        if port.port_type not in _DEVICE_FACING:
            continue
        switch_iface = interfaces.get(port.index)
        if switch_iface is None:
            continue

        # Port Index is the primary, reliable join key; fabric_port_name
        # (keyed off switchshow's own WWN column) is only a fallback for
        # entries that somehow lack a port_index.
        entries = by_port_index.get(port.index) or by_fabric_port_name.get(port.wwn or "", [])
        if not entries:
            continue
        ports_with_entries += 1

        logged_in_wwns = [normalize_wwn(e.port_name) for e in entries]
        resolved: list[tuple[str, object, str]] = []  # (wwn, netbox_iface, brocade_device_type)
        for entry in entries:
            wwn = _target_wwn_for_entry(entry)
            if not wwn:
                continue
            physical_iface, was_virtual_in_netbox = _resolve_physical_interface(nb, wwn)
            if physical_iface is not None:
                resolved.append((wwn, physical_iface, entry.device_type or ""))
                if was_virtual_in_netbox:
                    log.info(
                        "port %s: WWN %s is a virtual (NPIV) interface in NetBox -> resolved to "
                        "physical parent '%s'",
                        port.name, wwn, physical_iface.name,
                    )

        if not resolved:
            log.warning(
                "port %s: device(s) logged in on the switch (WWN%s: %s) but NONE of them exist "
                "as an Interface.wwn anywhere in NetBox -- no cable created. If you expect this "
                "device to already be in NetBox, check the WWN was entered exactly as shown here.",
                port.name, "s" if len(logged_in_wwns) > 1 else "", ", ".join(logged_in_wwns),
            )
            status_counts["not_found_in_netbox"] = status_counts.get("not_found_in_netbox", 0) + 1
            continue

        distinct_targets = {iface.id: (wwn, iface, devtype) for wwn, iface, devtype in resolved}
        if len(distinct_targets) > 1:
            # Genuine disagreement -- prefer an entry Brocade itself
            # labeled "Physical" over one labeled "NPIV", before falling
            # back to plain list order.
            physical_first = sorted(
                distinct_targets.values(),
                key=lambda t: 0 if "physical" in t[2].lower() else 1,
            )
            if "physical" in physical_first[0][2].lower():
                target_wwn, target_iface, _ = physical_first[0]
                log.info(
                    "port %s: %d different NetBox interfaces matched -- using the one Brocade "
                    "reports as physical (%s)",
                    port.name, len(distinct_targets), target_wwn,
                )
            elif config.sync.ambiguous_wwn_strategy == "skip":
                log.warning(
                    "port %s: %d different NetBox interfaces matched WWNs on this port and none "
                    "is Brocade-labeled physical -- ambiguous_wwn_strategy=skip, leaving uncabled",
                    port.name, len(distinct_targets),
                )
                status_counts["ambiguous_skipped"] = status_counts.get("ambiguous_skipped", 0) + 1
                continue
            else:
                target_wwn, target_iface, _ = resolved[0]
                log.warning(
                    "port %s: %d different NetBox interfaces matched WWNs on this port and none "
                    "is Brocade-labeled physical -- ambiguous_wwn_strategy=first, using first-listed WWN %s",
                    port.name, len(distinct_targets), target_wwn,
                )
        else:
            target_wwn, target_iface, _ = next(iter(distinct_targets.values()))

        nb_status = nb.ensure_cable(switch_iface, target_iface)
        status_counts[nb_status] = status_counts.get(nb_status, 0) + 1
        log.info("port %s -> target interface '%s' (WWN %s): %s", port.name, target_iface.name, target_wwn, nb_status)

    accounted_for = sum(status_counts.values())
    if accounted_for != ports_with_entries:
        # This should never happen -- it means some port fell through a
        # code path that didn't log or tally anything. Flag it loudly
        # rather than silently under-reporting, which is what happened
        # before this accounting check existed.
        log.warning(
            "connection sync accounting mismatch: %d ports had logged-in WWNs but only %d are "
            "accounted for in the summary below -- this points to a bug, please report it",
            ports_with_entries, accounted_for,
        )

    log.info(
        "connection sync summary (%d device-facing port(s) with logins): "
        "%d newly cabled, %d already connected, %d target already cabled elsewhere "
        "(duplicate WWN in NetBox), %d self-matched, %d same-switch-matched "
        "(duplicate WWN in NetBox), %d cable creation error(s), %d ambiguous-skipped, "
        "%d not found in NetBox",
        ports_with_entries,
        status_counts.get("created", 0),
        status_counts.get("already_connected", 0),
        status_counts.get("target_busy", 0),
        status_counts.get("self_match_skipped", 0),
        status_counts.get("same_device_skipped", 0),
        status_counts.get("error", 0),
        status_counts.get("ambiguous_skipped", 0),
        status_counts.get("not_found_in_netbox", 0),
    )
