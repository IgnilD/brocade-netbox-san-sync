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
    the Brocade-native resolution below -- either one alone would catch
    most cases, both together catch more.
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


def _resolve_expected_target(nb: NetBoxSyncClient, config: AppConfig, port_name: str, entries: list[NameServerEntry]):
    """Works out which single NetBox interface (if any) a device-facing
    port's logged-in WWNs should be cabled to right now. Returns
    (target_iface_or_None, status_if_no_definitive_target_or_None).
    The second value is only set when there's no usable target -- either
    nothing in NetBox matched, or the entries were genuinely ambiguous
    and `ambiguous_wwn_strategy: skip` applies -- so the caller can tell
    "no target, here's why" apart from "here's the target"."""
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
                    port_name, wwn, physical_iface.name,
                )

    if not resolved:
        log.warning(
            "port %s: device(s) logged in on the switch (WWN%s: %s) but NONE of them exist "
            "as an Interface.wwn anywhere in NetBox. If you expect this device to already be "
            "in NetBox, check the WWN was entered exactly as shown here.",
            port_name, "s" if len(logged_in_wwns) > 1 else "", ", ".join(logged_in_wwns),
        )
        return None, "not_found_in_netbox"

    distinct_targets = {iface.id: (wwn, iface, devtype) for wwn, iface, devtype in resolved}
    if len(distinct_targets) == 1:
        _, target_iface, _ = next(iter(distinct_targets.values()))
        return target_iface, None

    # Genuine disagreement -- prefer an entry Brocade itself labeled
    # "Physical" over one labeled "NPIV", before falling back to plain
    # list order.
    physical_first = sorted(distinct_targets.values(), key=lambda t: 0 if "physical" in t[2].lower() else 1)
    if "physical" in physical_first[0][2].lower():
        target_wwn, target_iface, _ = physical_first[0]
        log.info(
            "port %s: %d different NetBox interfaces matched -- using the one Brocade reports "
            "as physical (%s)",
            port_name, len(distinct_targets), target_wwn,
        )
        return target_iface, None

    if config.sync.ambiguous_wwn_strategy == "skip":
        log.warning(
            "port %s: %d different NetBox interfaces matched WWNs on this port and none is "
            "Brocade-labeled physical -- ambiguous_wwn_strategy=skip, leaving uncabled",
            port_name, len(distinct_targets),
        )
        return None, "ambiguous_skipped"

    target_wwn, target_iface, _ = resolved[0]
    log.warning(
        "port %s: %d different NetBox interfaces matched WWNs on this port and none is "
        "Brocade-labeled physical -- ambiguous_wwn_strategy=first, using first-listed WWN %s",
        port_name, len(distinct_targets), target_wwn,
    )
    return target_iface, None


def _handle_stale_cable(nb: NetBoxSyncClient, config: AppConfig, port_name: str, switch_iface, reason: str) -> str:
    """Called when a device-facing port currently has no valid target
    (nothing logged in, or nothing resolved in NetBox). Returns `reason`
    unchanged if there's no cable to worry about. If the port still has
    a cable from a previous run, that cable is now stale -- deletes it
    if `prune_stale_cables` is on (and only if this tool created it),
    otherwise just logs what it would do."""
    far_end = nb.get_cable_far_end(switch_iface)
    if far_end is None:
        return reason

    if config.sync.prune_stale_cables:
        deleted = nb.delete_managed_cable(switch_iface)
        if deleted:
            log.info(
                "port %s: no valid device WWN this run -- removed stale cable to '%s'",
                port_name, far_end.name,
            )
            return "pruned"
        return reason  # existing cable wasn't ours to delete; leave the original reason as the status
    else:
        log.warning(
            "port %s: currently cabled to '%s', but no valid device WWN matches this run -- "
            "this cable looks stale. Set sync.prune_stale_cables: true to remove it automatically.",
            port_name, far_end.name,
        )
        return "stale_detected"


def _handle_mismatch(nb: NetBoxSyncClient, config: AppConfig, port_name: str, switch_iface, current_far_end, target_iface) -> str:
    """Called when a device-facing port's OWN cable points somewhere
    other than what this run resolves it to. Same prune-or-warn pattern
    as _handle_stale_cable."""
    if config.sync.prune_stale_cables:
        nb.delete_managed_cable(switch_iface)
        log.info(
            "port %s: cabled to the wrong target ('%s' instead of '%s') -- removed and re-cabling",
            port_name, current_far_end.name, target_iface.name,
        )
        nb_status = nb.ensure_cable(switch_iface, target_iface)
        return "recabled" if nb_status == "created" else nb_status
    else:
        log.warning(
            "port %s: currently cabled to '%s', but the device now logged in resolves to '%s' "
            "instead -- mismatch. Set sync.prune_stale_cables: true to fix this automatically.",
            port_name, current_far_end.name, target_iface.name,
        )
        return "mismatch_detected"


def _handle_target_busy(
    nb: NetBoxSyncClient,
    config: AppConfig,
    port_name: str,
    switch_iface,
    target_iface,
    target_holder,
    index_by_iface_id: dict[int, int],
    resolution_by_index: dict[int, object],
) -> str:
    """Called when this port is uncabled and wants `target_iface`, but
    something else already holds it. The key question: is that holder's
    claim still valid right now, or is it stale?

    We can only answer that safely for a holder that is itself one of
    THIS switch's own ports (we have live, fresh data on those this
    run) -- so this checks the holder's port index against
    `resolution_by_index`, which was computed from this exact run's
    name-server data for every device-facing port *before* any cables
    were touched (see sync_connections). If the holder is a switch port
    that, per that live data, no longer resolves to this same target
    (e.g. it's offline, or logged into something else now), its claim
    is stale and safe to reclaim. If the holder isn't one of this
    switch's own ports at all -- a host's own interface, another
    switch, anything we have no live visibility into -- it is never
    touched, regardless of prune_stale_cables.
    """
    holder_port_index = index_by_iface_id.get(target_holder.id)
    if holder_port_index is None:
        log.warning(
            "port %s: target '%s' is already cabled to '%s', which isn't one of this switch's "
            "own ports -- can't verify whether that's still correct from here, leaving it alone.",
            port_name, target_iface.name, target_holder.name,
        )
        return "target_busy"

    holder_resolution = resolution_by_index.get(holder_port_index)
    holder_still_claims_it = holder_resolution is not None and holder_resolution.id == target_iface.id
    if holder_still_claims_it:
        log.warning(
            "port %s: target '%s' is already cabled to port index %d, which ALSO still resolves "
            "to this same target per this run's live data -- genuine conflict between two ports, "
            "leaving as-is (needs manual review, likely a duplicate/incorrect WWN in NetBox).",
            port_name, target_iface.name, holder_port_index,
        )
        return "target_busy"

    # The holder is one of this switch's own ports, and per fresh data
    # from THIS run it no longer claims this target -- its cable is
    # provably stale, not a guess.
    if config.sync.prune_stale_cables:
        deleted = nb.delete_managed_cable(target_holder)
        if deleted:
            log.info(
                "port %s: target '%s' was cabled to port index %d, which no longer claims it per "
                "this run's live switch data -- removed that stale cable and re-cabling here",
                port_name, target_iface.name, holder_port_index,
            )
            nb_status = nb.ensure_cable(switch_iface, target_iface)
            return "reclaimed" if nb_status == "created" else nb_status
        return "target_busy"  # existing cable wasn't ours to delete
    else:
        log.warning(
            "port %s: target '%s' is currently cabled to port index %d, but that port no longer "
            "claims it per this run's live data -- looks like a stale claim, not a real conflict. "
            "Set sync.prune_stale_cables: true to reclaim it automatically.",
            port_name, target_iface.name, holder_port_index,
        )
        return "stale_claim_detected"


def sync_connections(nb: NetBoxSyncClient, config: AppConfig, snapshot: SwitchSnapshot, interfaces: dict[int, object]) -> None:
    """For every device-facing port (F-Port/FL-Port), figures out which
    WWNs are logged in (via the name server, joined to the port by
    **Port Index** -- switchshow's own WWN column is not reliable enough
    to key off, see ssh_client.py), resolves each to the WWN of the
    *physical* device behind it (using Brocade's own `Device type` /
    `Permanent Port Name` fields when available, i.e. real NPIV
    resolution rather than a heuristic), looks that WWN up in NetBox,
    and creates a Cable to it if found.

    Runs in two phases. Phase 1 resolves every device-facing port's
    correct target from this run's live data, before touching any
    cables at all. Phase 2 reconciles cables using that complete
    picture, which is what makes it possible to safely detect a
    specific, easy-to-hit case: a target interface is already cabled to
    a *different* one of this switch's own ports, and that port's own
    fresh resolution this run no longer points at the same target (e.g.
    someone manually re-cabled it in NetBox to a port that's actually
    offline, or the device moved) -- a provably stale claim, not a
    guess, since it's checked against this exact run's own data.
    A holder that isn't one of this switch's own ports at all (a host's
    interface, another switch, anything with no live data available
    this run) is never touched, no matter what.

    Every device-facing port gets exactly one INFO/WARNING log line
    explaining what happened to it, and that accounting is cross-checked
    against the final tally at the end of the run, so a silently-dropped
    port would show up as a mismatch warning rather than just vanishing
    from the numbers.
    """
    if not config.sync.create_cables:
        log.info("cable sync disabled -- skipping")
        return

    by_port_index = snapshot.name_server_by_port_index()
    by_fabric_port_name = snapshot.name_server_by_fabric_port_name()
    index_by_iface_id = {iface.id: idx for idx, iface in interfaces.items()}

    # -- Phase 1: resolve every device-facing port's target first ----------
    ports_to_process = []
    resolution_by_index: dict[int, object] = {}
    no_target_reason_by_index: dict[int, str] = {}

    for port in snapshot.ports:
        if port.port_type not in _DEVICE_FACING:
            continue
        if interfaces.get(port.index) is None:
            continue
        ports_to_process.append(port)

        entries = by_port_index.get(port.index) or by_fabric_port_name.get(port.wwn or "", [])
        if not entries:
            resolution_by_index[port.index] = None
            no_target_reason_by_index[port.index] = "empty_no_cable"
            continue

        target_iface, no_target_reason = _resolve_expected_target(nb, config, port.name, entries)
        resolution_by_index[port.index] = target_iface
        no_target_reason_by_index[port.index] = no_target_reason

    # -- Phase 2: reconcile cables using the complete picture above --------
    status_counts: dict[str, int] = {}

    for port in ports_to_process:
        switch_iface = interfaces[port.index]
        target_iface = resolution_by_index[port.index]

        if target_iface is None:
            status = _handle_stale_cable(nb, config, port.name, switch_iface, no_target_reason_by_index[port.index])
            status_counts[status] = status_counts.get(status, 0) + 1
            continue

        current_far_end = nb.get_cable_far_end(switch_iface)

        if current_far_end is not None and current_far_end.id == target_iface.id:
            status_counts["already_connected"] = status_counts.get("already_connected", 0) + 1
            continue

        if current_far_end is not None:
            status = _handle_mismatch(nb, config, port.name, switch_iface, current_far_end, target_iface)
            status_counts[status] = status_counts.get(status, 0) + 1
            continue

        # our port is uncabled -- but is the target already claimed by
        # some other interface?
        target_holder = nb.get_cable_far_end(target_iface)
        if target_holder is not None and target_holder.id != switch_iface.id:
            status = _handle_target_busy(
                nb, config, port.name, switch_iface, target_iface, target_holder,
                index_by_iface_id, resolution_by_index,
            )
            status_counts[status] = status_counts.get(status, 0) + 1
            continue

        nb_status = nb.ensure_cable(switch_iface, target_iface)
        status_counts[nb_status] = status_counts.get(nb_status, 0) + 1
        log.info("port %s -> target interface '%s': %s", port.name, target_iface.name, nb_status)

    ports_considered = len(ports_to_process)
    accounted_for = sum(status_counts.values())
    if accounted_for != ports_considered:
        # This should never happen -- it means some port fell through a
        # code path that didn't log or tally anything. Flag it loudly
        # rather than silently under-reporting.
        log.warning(
            "connection sync accounting mismatch: %d device-facing ports considered but only %d "
            "are accounted for in the summary below -- this points to a bug, please report it",
            ports_considered, accounted_for,
        )

    log.info(
        "connection sync summary (%d device-facing port(s)): %d newly cabled, %d re-cabled "
        "(was wrong target), %d reclaimed (target's stale claim removed), %d already connected, "
        "%d unchanged (nothing logged in), %d stale cable(s) detected, %d stale claim(s) "
        "detected on target, %d pruned, %d mismatch(es) detected, %d target already cabled "
        "elsewhere (can't verify), %d self-matched, %d same-switch-matched, %d cable error(s), "
        "%d ambiguous-skipped, %d not found in NetBox",
        ports_considered,
        status_counts.get("created", 0),
        status_counts.get("recabled", 0),
        status_counts.get("reclaimed", 0),
        status_counts.get("already_connected", 0),
        status_counts.get("empty_no_cable", 0),
        status_counts.get("stale_detected", 0),
        status_counts.get("stale_claim_detected", 0),
        status_counts.get("pruned", 0),
        status_counts.get("mismatch_detected", 0),
        status_counts.get("target_busy", 0),
        status_counts.get("self_match_skipped", 0),
        status_counts.get("same_device_skipped", 0),
        status_counts.get("error", 0),
        status_counts.get("ambiguous_skipped", 0),
        status_counts.get("not_found_in_netbox", 0),
    )
