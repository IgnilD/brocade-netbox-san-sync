"""
REST parser tests built from Broadcom's own documented example response
bodies (techdocs.broadcom.com, FOS 9.2.x REST API Reference Manual),
translated from their XML examples into the equivalent JSON shape that
`Accept: application/yang-data+json` returns -- not invented data. These
exist specifically to catch the two real bugs found in an earlier
version of this client: (1) every response is wrapped in a top-level
"Response" object that was never being unwrapped, and (2) several
name-server field names were guessed wrong (fabric-port-name,
permanent-port-name, name-server-device-type).
"""
from unittest.mock import MagicMock, patch

from app.brocade.models import PortState, PortType
from app.brocade.rest_client import BrocadeRESTClient


def _client() -> BrocadeRESTClient:
    return BrocadeRESTClient(host="10.20.20.11", username="admin", password="x")


def _mock_get_response(json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    return resp


# Directly from Broadcom's documented "Retrieving Name Server
# Information for a Switch" example response, translated from XML to
# the equivalent JSON shape.
NAME_SERVER_RESPONSE = {
    "Response": {
        "fibrechannel-name-server": [
            {
                "port-id": "0x2b9200",
                "port-name": "10:00:00:10:9b:8f:2b:c7",
                "port-symbolic-name": '[34] "Emulex PPN-10:00:00:10:9B:8F:2B:C7"',
                "fabric-port-name": "20:92:c4:f5:7c:4a:ac:6c",
                "permanent-port-name": "10:00:00:10:9b:8f:2b:c7",
                "node-name": "20:00:00:10:9b:8f:2b:c7",
                "node-symbolic-name": '[76] "Emulex LPe35002-M2 FV12.8.351.6901"',
                "class-of-service": "class-2 class-3",
                "fc4-type": "FCP",
                "fc4-features": "FCP-Initiator",
                "port-type": "n-port",
                "name-server-device-type": "Physical Initiator",
                "port-index": 146,
                "link-speed": "32G",
                "protocol-speed": "32-gfc",
            },
            {
                "port-id": "0xee2a00",
                "port-name": "20:28:d8:1f:cc:5a:5d:e8",
                "fabric-port-name": "20:2a:d8:1f:cc:5a:f8:4e",
                "permanent-port-name": "20:28:d8:1f:cc:5a:5d:e8",
                "node-name": "10:00:d8:1f:cc:5a:5d:e8",
                "port-type": "n-port",
                "name-server-device-type": "Physical Unknown(initiator/target)",
                "port-index": 42,
                "link-speed": "64G",
                "protocol-speed": "64-gfc",
            },
        ]
    }
}


def test_rest_name_server_unwraps_response_and_uses_correct_field_names():
    client = _client()
    client._session = MagicMock()
    client._session.get.return_value = _mock_get_response(NAME_SERVER_RESPONSE)

    entries = client._get_name_server()

    assert len(entries) == 2
    e0 = entries[0]
    assert e0.port_id == "0x2b9200"
    assert e0.port_name == "10:00:00:10:9b:8f:2b:c7"
    # these three were the actually-broken fields in the earlier version
    assert e0.fabric_port_name == "20:92:c4:f5:7c:4a:ac:6c"
    assert e0.permanent_port_name == "10:00:00:10:9b:8f:2b:c7"
    assert e0.device_type == "Physical Initiator"
    assert e0.port_index == 146
    assert not e0.is_reported_npiv


# Representative brocade-interface/fibrechannel row using CURRENT
# (non-deprecated) field names per Broadcom's module tree.
INTERFACE_RESPONSE = {
    "Response": {
        "fibrechannel": [
            {
                "name": "0/16",
                "wwn": "20:10:00:05:33:88:72:30",
                "index": 16,
                "is-enabled-state": True,
                "physical-state": "online",
                "port-type-string": "f-port",
                "protocol-speed": "8-gfc",
                "npiv-enabled-v2": True,
                "user-friendly-name": "",
            }
        ]
    }
}


def test_rest_interface_parses_current_field_names():
    client = _client()
    client._session = MagicMock()
    client._session.get.return_value = _mock_get_response(INTERFACE_RESPONSE)

    ports = client._get_ports()

    assert len(ports) == 1
    p = ports[0]
    assert p.index == 16
    assert p.wwn == "20:10:00:05:33:88:72:30"
    assert p.state == PortState.ONLINE
    assert p.port_type == PortType.F_PORT
    assert p.speed_gbps == 8
    assert p.enabled is True
    assert p.npiv_enabled is True


# Same row but using the OLDER/deprecated field names, to confirm the
# fallback chain works for switches running older FOS builds.
INTERFACE_RESPONSE_OLD_FIELDS = {
    "Response": {
        "fibrechannel": [
            {
                "name": "0/16",
                "wwn": "20:10:00:05:33:88:72:30",
                "index": 16,
                "enabled-state": 1,
                "physical-state": "online",
                "port-type": "f-port",
                "npiv-enabled": 1,
            }
        ]
    }
}


def test_rest_interface_falls_back_to_older_field_names():
    client = _client()
    client._session = MagicMock()
    client._session.get.return_value = _mock_get_response(INTERFACE_RESPONSE_OLD_FIELDS)

    ports = client._get_ports()

    assert len(ports) == 1
    p = ports[0]
    assert p.state == PortState.ONLINE
    assert p.port_type == PortType.F_PORT
    assert p.enabled is True
    assert p.npiv_enabled is True


# brocade-chassis/chassis and brocade-fibrechannel-switch/fibrechannel-switch.
# Values here match a real report: REST's "model" field is NOT a
# friendly name, it's the same raw switchType-style value the SSH CLI's
# switchshow reports (e.g. "162.5" for a G620) -- an earlier version of
# this client used it as-is instead of resolving it through the same
# lookup table the SSH path already used.
SWITCH_INFO_RESPONSE = {
    "Response": {
        "fibrechannel-switch": [
            {
                "name": "10:00:00:05:33:88:72:30",
                "domain-id": 1,
                "user-friendly-name": "f1sw1-test",
                "model": "162.5",
                "firmware-version": "v9.1.1b",
                "fabric-user-friendly-name": "Fabric-Prod-A",
            }
        ]
    }
}
CHASSIS_RESPONSE = {
    "Response": {
        "chassis": {
            "chassis-wwn": "10:00:00:05:33:88:72:30",
            "product-name": "162.5",
            "part-number": "80-1010119-01",
            "serial-number": "EWY1948Q00P",
        }
    }
}
MGMT_INTERFACE_RESPONSE = {
    "Response": {
        "management-ethernet-interface": [
            {
                "cp-name": "CP0",
                "interface-name": "eth0",
                "inet-address": "10.18.3.22",
                "subnet-mask": "255.255.255.0",
            }
        ]
    }
}


def test_rest_switch_info_resolves_friendly_model_and_unwraps_all_resources():
    client = _client()
    client._session = MagicMock()
    client._session.get.side_effect = [
        _mock_get_response(SWITCH_INFO_RESPONSE),
        _mock_get_response(CHASSIS_RESPONSE),
        _mock_get_response(MGMT_INTERFACE_RESPONSE),
    ]

    info = client._get_switch_info()

    assert info.name == "f1sw1-test"
    assert info.domain_id == 1
    assert info.firmware == "v9.1.1b"
    assert info.serial_number == "EWY1948Q00P"
    assert info.fabric_name == "Fabric-Prod-A"
    assert info.mgmt_ip == "10.18.3.22"
    assert info.mgmt_prefix_len == 24
    # the actual point of this test: REST's raw "162.5" resolves to a
    # real product name via the same table the SSH path uses, instead
    # of being used as-is
    assert info.switch_type == "162.5"
    assert info.model == "Brocade G620"
    assert info.part_number == "80-1010119-01"
