"""
Tests for stale-cable detection and pruning in connection_sync.py /
NetBoxSyncClient.delete_managed_cable(). Core safety property under
test: a cable is only ever auto-deleted if (a) sync.prune_stale_cables
is explicitly enabled, and (b) the cable itself carries this tool's own
managed_tag -- a cable created by hand is never touched, no matter how
"stale" it looks.
"""
from unittest.mock import MagicMock, patch

from app.brocade.models import NameServerEntry, PortInfo, PortType, SwitchInfo, SwitchSnapshot
from app.config import AppConfig, NetBoxConfig, SyncOptions
from app.netbox.client import NetBoxSyncClient
from app.sync.connection_sync import sync_connections


def _snapshot_one_empty_port() -> SwitchSnapshot:
    """One F-Port with nothing currently logged in (device disconnected)."""
    ports = [PortInfo(index=2, name="port2", port_type=PortType.F_PORT, wwn="aa")]
    return SwitchSnapshot(switch=SwitchInfo(name="sw01", wwn="x"), ports=ports, name_server=[])


def test_stale_cable_is_only_warned_about_by_default():
    switch_iface = MagicMock(id=100)
    switch_iface.name = "port2"
    stale_target = MagicMock(id=500)
    stale_target.name = "decommissioned-host-hba0"

    nb = MagicMock()
    nb.get_cable_far_end.return_value = stale_target

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=False))
    sync_connections(nb, cfg, _snapshot_one_empty_port(), {2: switch_iface})

    nb.delete_managed_cable.assert_not_called()


def test_stale_cable_is_deleted_when_pruning_enabled():
    switch_iface = MagicMock(id=100)
    switch_iface.name = "port2"
    stale_target = MagicMock(id=500)
    stale_target.name = "decommissioned-host-hba0"

    nb = MagicMock()
    nb.get_cable_far_end.return_value = stale_target
    nb.delete_managed_cable.return_value = True

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=True))
    sync_connections(nb, cfg, _snapshot_one_empty_port(), {2: switch_iface})

    nb.delete_managed_cable.assert_called_once_with(switch_iface)


def test_no_cable_action_taken_when_nothing_is_cabled():
    switch_iface = MagicMock(id=100)
    switch_iface.name = "port2"

    nb = MagicMock()
    nb.get_cable_far_end.return_value = None  # nothing currently cabled

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=True))
    sync_connections(nb, cfg, _snapshot_one_empty_port(), {2: switch_iface})

    nb.delete_managed_cable.assert_not_called()
    nb.ensure_cable.assert_not_called()


def test_delete_managed_cable_refuses_a_cable_it_did_not_create():
    """The actual safety check, tested directly against
    NetBoxSyncClient rather than through the mocked sync_connections
    flow above -- this is what makes it safe for prune_stale_cables to
    default off being merely a convenience, not the only thing
    protecting hand-made cables from deletion."""
    fake_managed_tag = MagicMock(id=1)
    fake_managed_tag.name = "brocade-sync"

    with patch("pynetbox.api") as mock_api:
        instance = mock_api.return_value
        instance.extras.tags.get.return_value = fake_managed_tag
        cfg = AppConfig(netbox=NetBoxConfig(url="https://netbox.example.com", token="x", verify_tls=False), switches=[], sync=SyncOptions())
        client = NetBoxSyncClient(cfg)

        iface = MagicMock(id=100)
        iface.name = "port2"
        iface.cable = {"id": 999}
        manual_cable = MagicMock(id=999)
        human_made_tag = MagicMock()
        human_made_tag.name = "manually-cabled"  # NOT this tool's managed_tag
        manual_cable.tags = [human_made_tag]

        instance.dcim.interfaces.get.return_value = iface
        instance.dcim.cables.get.return_value = manual_cable

        result = client.delete_managed_cable(iface)

        assert result is False
        manual_cable.delete.assert_not_called()


def test_delete_managed_cable_deletes_when_tag_matches():
    fake_managed_tag = MagicMock(id=1)
    fake_managed_tag.name = "brocade-sync"

    with patch("pynetbox.api") as mock_api:
        instance = mock_api.return_value
        instance.extras.tags.get.return_value = fake_managed_tag
        cfg = AppConfig(netbox=NetBoxConfig(url="https://netbox.example.com", token="x", verify_tls=False), switches=[], sync=SyncOptions())
        client = NetBoxSyncClient(cfg)

        iface = MagicMock(id=100)
        iface.name = "port2"
        iface.cable = {"id": 999}
        managed_cable = MagicMock(id=999)
        managed_cable.tags = [fake_managed_tag]  # this tool's own tag

        instance.dcim.interfaces.get.return_value = iface
        instance.dcim.cables.get.return_value = managed_cable

        result = client.delete_managed_cable(iface)

        assert result is True
        managed_cable.delete.assert_called_once()


def test_unknown_wwn_is_never_coerced_to_empty_string():
    """Regression test for a real bug: an unknown port WWN (e.g. an
    offline port that was never fetched via portshow) used to be sent
    as wwn="" instead of being omitted, which fought with NetBox's
    nullable wwn field and caused every single sync to show a false
    "updated" diff (null -> "" -> null -> "" ...) even when nothing
    about the port had actually changed."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from app.brocade.models import PortInfo, PortState
    from app.config import AppConfig, NetBoxConfig, SwitchConfig, SyncOptions
    from app.netbox.client import NetBoxSyncClient

    fake_tag = MagicMock(id=1)
    with patch("pynetbox.api") as mock_api:
        instance = mock_api.return_value
        instance.extras.tags.get.return_value = fake_tag
        cfg = AppConfig(netbox=NetBoxConfig(url="https://netbox.example.com", token="x", verify_tls=False), switches=[], sync=SyncOptions())
        client = NetBoxSyncClient(cfg)
        client._interface_type_cache = {"other"}

        device = MagicMock(id=1)
        switch_cfg = SwitchConfig(name="sw01", host="x", username="x", password="x")

        # represents an interface as it currently exists in NetBox --
        # wwn is null there, matching a port whose WWN was never
        # determined (e.g. offline, so portshow was never queried)
        existing_iface = SimpleNamespace(id=50, wwn=None, save=lambda: None)
        instance.dcim.interfaces.get.return_value = existing_iface

        offline_port = PortInfo(index=39, name="port39", state=PortState.OFFLINE, wwn=None)
        client.get_or_create_port_interface(device, offline_port, switch_cfg)

        assert existing_iface.wwn is None


# --- regression tests for target-side stale-claim reclaiming ---------------
#
# Real scenario this covers: a device is physically connected to port44.
# Someone manually re-points its NetBox Cable to "port49" in the UI, but
# port49 is a U-Port with nothing plugged in -- the switch itself has
# never logged anything in on port49. Every run, port44 correctly
# resolves its target, finds it already claimed by port49, and (before
# this fix) just gave up every time because port49's cable technically
# came from this tool. Now it cross-checks port49's OWN fresh resolution
# this run -- since port49 isn't even device-facing right now, its claim
# is provably stale, and it's safe to reclaim.

from app.brocade.models import NameServerEntry


def _snapshot_two_ports(port44_entries, port49_type):
    """port44: F-Port with device(s) logged in (from `port44_entries`).
    port49: whatever port_type is passed -- PortType.U_PORT for "empty,
    never logged in", or PortType.F_PORT with entries for "genuinely
    still claims it"."""
    ports = [
        PortInfo(index=44, name="port44", port_type=PortType.F_PORT, wwn="aa"),
        PortInfo(index=49, name="port49", port_type=port49_type, wwn="bb"),
    ]
    return ports


def test_target_busy_is_reclaimed_when_holder_no_longer_claims_it():
    """The exact reported scenario: target's current holder (port49) is
    not even device-facing this run (a U-Port, nothing logged in) --
    its claim is stale and gets reclaimed when pruning is enabled."""
    switch_iface_44 = MagicMock(id=144)
    switch_iface_44.name = "port44"
    switch_iface_49 = MagicMock(id=149)
    switch_iface_49.name = "port49"
    target_iface = MagicMock(id=999)
    target_iface.name = "CTE0.B.IOM0.P2"
    target_iface.parent = None

    ports = _snapshot_two_ports(None, PortType.U_PORT)  # port49 = U-Port, no entries at all
    entries = [NameServerEntry(port_id="1", port_name="21:00:00:00:aa:bb:cc:99", node_name="x", port_index=44, device_type="Physical Initiator")]
    snapshot = SwitchSnapshot(switch=SwitchInfo(name="sw01", wwn="x"), ports=ports, name_server=entries)
    interfaces = {44: switch_iface_44, 49: switch_iface_49}

    nb = MagicMock()
    nb.find_interface_by_wwn.return_value = target_iface
    # port44 is uncabled; the target is currently held by port49
    nb.get_cable_far_end.side_effect = lambda iface: (
        None if iface.id == 144 else switch_iface_49 if iface.id == 999 else None
    )
    nb.delete_managed_cable.return_value = True
    nb.ensure_cable.return_value = "created"

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=True))
    sync_connections(nb, cfg, snapshot, interfaces)

    nb.delete_managed_cable.assert_called_once_with(switch_iface_49)
    nb.ensure_cable.assert_called_once_with(switch_iface_44, target_iface)


def test_target_busy_only_warns_when_pruning_disabled():
    switch_iface_44 = MagicMock(id=144)
    switch_iface_44.name = "port44"
    switch_iface_49 = MagicMock(id=149)
    switch_iface_49.name = "port49"
    target_iface = MagicMock(id=999)
    target_iface.name = "CTE0.B.IOM0.P2"
    target_iface.parent = None

    ports = _snapshot_two_ports(None, PortType.U_PORT)
    entries = [NameServerEntry(port_id="1", port_name="21:00:00:00:aa:bb:cc:99", node_name="x", port_index=44, device_type="Physical Initiator")]
    snapshot = SwitchSnapshot(switch=SwitchInfo(name="sw01", wwn="x"), ports=ports, name_server=entries)
    interfaces = {44: switch_iface_44, 49: switch_iface_49}

    nb = MagicMock()
    nb.find_interface_by_wwn.return_value = target_iface
    nb.get_cable_far_end.side_effect = lambda iface: (
        None if iface.id == 144 else switch_iface_49 if iface.id == 999 else None
    )

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=False))
    sync_connections(nb, cfg, snapshot, interfaces)

    nb.delete_managed_cable.assert_not_called()
    nb.ensure_cable.assert_not_called()


def test_target_busy_left_alone_when_holder_still_genuinely_claims_it():
    """If the current holder is ALSO device-facing and its own fresh
    resolution this run still points at the same target, that's a real
    conflict (e.g. duplicate WWN data), not staleness -- must never be
    auto-resolved, even with pruning on."""
    switch_iface_44 = MagicMock(id=144)
    switch_iface_44.name = "port44"
    switch_iface_49 = MagicMock(id=149)
    switch_iface_49.name = "port49"
    target_iface = MagicMock(id=999)
    target_iface.name = "CTE0.B.IOM0.P2"
    target_iface.parent = None

    ports = _snapshot_two_ports(None, PortType.F_PORT)  # port49 is ALSO F-Port with entries
    entries = [
        NameServerEntry(port_id="1", port_name="21:00:00:00:aa:bb:cc:99", node_name="x", port_index=44, device_type="Physical Initiator"),
        NameServerEntry(port_id="2", port_name="21:00:00:00:aa:bb:cc:99", node_name="x", port_index=49, device_type="Physical Initiator"),
    ]
    snapshot = SwitchSnapshot(switch=SwitchInfo(name="sw01", wwn="x"), ports=ports, name_server=entries)
    interfaces = {44: switch_iface_44, 49: switch_iface_49}

    nb = MagicMock()
    nb.find_interface_by_wwn.return_value = target_iface  # both ports resolve to the same target
    # bidirectionally consistent: target_iface's far end is port49, and
    # port49's OWN far end is (reciprocally) target_iface -- they're the
    # two ends of one real cable
    nb.get_cable_far_end.side_effect = lambda iface: (
        None if iface.id == 144
        else switch_iface_49 if iface.id == 999
        else target_iface if iface.id == 149
        else None
    )

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=True))
    sync_connections(nb, cfg, snapshot, interfaces)

    # genuine conflict -- must NOT delete or recable anything automatically
    nb.delete_managed_cable.assert_not_called()
    nb.ensure_cable.assert_not_called()


def test_target_busy_holder_outside_this_switch_is_never_touched():
    """If the target's current holder isn't one of THIS switch's own
    ports at all (e.g. a host's own interface), there's no live data to
    judge it by -- never touched, regardless of pruning."""
    switch_iface_44 = MagicMock(id=144)
    switch_iface_44.name = "port44"
    target_iface = MagicMock(id=999)
    target_iface.name = "CTE0.B.IOM0.P2"
    target_iface.parent = None
    unrelated_holder = MagicMock(id=55555)  # not in `interfaces` at all
    unrelated_holder.name = "some-hosts-own-hba1"

    ports = [PortInfo(index=44, name="port44", port_type=PortType.F_PORT, wwn="aa")]
    entries = [NameServerEntry(port_id="1", port_name="21:00:00:00:aa:bb:cc:99", node_name="x", port_index=44, device_type="Physical Initiator")]
    snapshot = SwitchSnapshot(switch=SwitchInfo(name="sw01", wwn="x"), ports=ports, name_server=entries)
    interfaces = {44: switch_iface_44}  # note: nothing at index 49 here

    nb = MagicMock()
    nb.find_interface_by_wwn.return_value = target_iface
    nb.get_cable_far_end.side_effect = lambda iface: (
        None if iface.id == 144 else unrelated_holder if iface.id == 999 else None
    )

    cfg = AppConfig(netbox=NetBoxConfig(url="x", token="x"), switches=[], sync=SyncOptions(prune_stale_cables=True))
    sync_connections(nb, cfg, snapshot, interfaces)

    nb.delete_managed_cable.assert_not_called()
    nb.ensure_cable.assert_not_called()
