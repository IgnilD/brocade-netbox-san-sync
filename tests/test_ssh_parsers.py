"""
Parser tests built from real `switchshow` / `nsshow -t` output captured
off an actual Brocade switch (a Brocade 5100, switchType 66.1, domain 6,
hostname tf1sw1) -- not invented sample text. If you change the regexes
in ssh_client.py, these should keep passing; if your own switch's
output differs from this and a test breaks, that's real signal about
what to adjust, not a false alarm.
"""
from app.brocade.ssh_client import BrocadeSSHClient
from app.brocade.models import PortState, PortType
from app.brocade.switch_type_lookup import model_for_switch_type

# Real switchshow output. WWNs on ports the user didn't want to publish
# were redacted in the original to "SOME_WWN" (deliberately NOT a valid
# WWN pattern) -- that's kept here on purpose, since it's a real-world
# case the parser has to tolerate gracefully (no crash, wwn=None,
# attachment_note captures the literal text).
SWITCHSHOW_SAMPLE = """\
switchName:\ttf1sw1
switchType:\t66.1
switchState:\tOnline   
switchMode:\tNative
switchRole:\tPrincipal
switchDomain:\t6
switchId:\tfffc06
switchWwn:\t10:00:00:05:33:88:72:30
zoning:\t\tON (f1_v1)
switchBeacon:\tOFF
FC Router:\tOFF
HIF Mode:\tOFF
Allow XISL Use:\tOFF
LS Attributes:\t[FID: 128, Base Switch: No, Default Switch: Yes, Address Mode 0]

Index Port Address Media Speed       State   Proto
==================================================
   0   0   060000   --    N8\t   No_Module   FC  
   1   1   060100   --    N8\t   No_Module   FC  
   2   2   060200   id    N8\t   Online      FC  F-Port  1 N Port + 1 NPIV public 
   3   3   060300   id    N8\t   Online      FC  F-Port  1 N Port + 1 NPIV public 
   4   4   060400   --    N8\t   No_Module   FC  
   6   6   060600   id    N8\t   No_Light    FC  
   8   8   060800   id    N8\t   Online      FC  F-Port  21:00:f4:e9:d4:54:b1:ec 
  28  28   061c00   id    N8\t   Online      FC  F-Port  SOME_WWN
"""

# Real nsshow -t output (trimmed to a representative subset: one F-Port
# with a physical+NPIV pair sharing a Port Index, and one plain
# physical-initiator-only port). Entries are NOT blank-line-separated on
# this switch -- that's the bug this sample specifically guards against.
NSSHOW_SAMPLE = """\
{
 Type Pid    COS     PortName                NodeName                 TTL(sec)
 N    060200;      3;50:05:07:68:0b:21:5f:5e;50:05:07:68:0b:00:5f:5e; na
    FC4s: FCP [IBM     2145            0000]
    Fabric Port Name: 20:02:00:05:33:88:72:30 
    Permanent Port Name: 50:05:07:68:0b:21:5f:5e
    Device type: Physical Initiator+Target
    Port Index: 2
    Share Area: No
    Device Shared in Other AD: No
    Redirect: No 
    Partial: No
    LSAN: No
 N    060201;      3;50:05:07:68:0b:25:5f:5e;50:05:07:68:0b:00:5f:5e; na
    FC4s: FCP 
    Fabric Port Name: 20:02:00:05:33:88:72:30 
    Permanent Port Name: 50:05:07:68:0b:21:5f:5e
    Device type: NPIV Target
    Port Index: 2
    Share Area: No
    Device Shared in Other AD: No
    Redirect: No 
    Partial: No
    LSAN: No
 N    060800;      3;21:00:f4:e9:d4:54:b1:ec;20:00:f4:e9:d4:54:b1:ec; na
    FC4s: FCP 
    NodeSymb: [33] "QLE2692 FW:v9.08.02 DVR:v5.4.84.0"
    Fabric Port Name: 20:08:00:05:33:88:72:30 
    Permanent Port Name: 21:00:f4:e9:d4:54:b1:ec
    Device type: Physical Initiator
    Port Index: 8
    Share Area: No
    Device Shared in Other AD: No
    Redirect: No 
    Partial: No
    LSAN: No
The Local Name Server has 15 entries }
"""


def test_parse_switchshow_ports_and_switch_info():
    client = BrocadeSSHClient(host="10.10.10.11", username="admin", password="x")
    switch_info, ports = client._parse_switchshow(SWITCHSHOW_SAMPLE)

    assert switch_info.name == "tf1sw1"
    assert switch_info.wwn == "10:00:00:05:33:88:72:30"
    assert switch_info.domain_id == 6
    assert switch_info.switch_type == "66.1"
    assert model_for_switch_type(switch_info.switch_type) == "Brocade 5100"

    by_index = {p.index: p for p in ports}
    assert by_index[0].state == PortState.OFFLINE  # No_Module

    p2 = by_index[2]
    assert p2.state == PortState.ONLINE
    assert p2.port_type == PortType.F_PORT
    assert p2.speed_gbps == 8
    # switchshow's trailing text here is a free-text NPIV summary, NOT a
    # WWN -- must not be mistaken for one
    assert p2.wwn is None
    assert p2.attachment_note == "1 N Port + 1 NPIV public"

    p6 = by_index[6]
    assert p6.state == PortState.NO_LIGHT

    p8 = by_index[8]
    # switchshow's trailing WWN here is the ATTACHED DEVICE's WWN, not
    # the switch's own port WWN (confirmed against a real `portshow`
    # output showing these as two different values) -- switchshow
    # parsing alone must never populate PortInfo.wwn with it, only
    # attachment_note. The switch's own WWN is fetched separately via
    # `portshow <index>`, see test_apply_own_port_wwns_uses_portshow_not_switchshow.
    assert p8.wwn is None
    assert p8.attachment_note == "21:00:f4:e9:d4:54:b1:ec"

    p28 = by_index[28]
    # redacted placeholder text -- must not crash, must not look like a WWN
    assert p28.wwn is None
    assert p28.attachment_note == "SOME_WWN"


def test_parse_nsshow_npiv_resolves_to_same_physical_wwn():
    client = BrocadeSSHClient(host="10.10.10.11", username="admin", password="x")
    entries = client._parse_nsshow(NSSHOW_SAMPLE)

    assert len(entries) == 3
    by_pid = {e.port_id: e for e in entries}

    physical = by_pid["060200"]
    assert physical.device_type == "Physical Initiator+Target"
    assert physical.port_index == 2
    assert not physical.is_reported_npiv
    assert physical.resolved_physical_wwn == "50:05:07:68:0b:21:5f:5e"

    npiv = by_pid["060201"]
    assert npiv.device_type == "NPIV Target"
    assert npiv.port_index == 2
    assert npiv.is_reported_npiv
    assert npiv.permanent_port_name == "50:05:07:68:0b:21:5f:5e"
    # this is the key behavior: the NPIV entry's *resolved* WWN must
    # equal the physical entry's own WWN, so both collapse to one cable
    # target instead of creating a false ambiguity
    assert npiv.resolved_physical_wwn == physical.resolved_physical_wwn

    initiator_only = by_pid["060800"]
    assert initiator_only.device_type == "Physical Initiator"
    assert initiator_only.port_index == 8
    assert not initiator_only.is_reported_npiv
    assert initiator_only.resolved_physical_wwn == "21:00:f4:e9:d4:54:b1:ec"


def test_name_server_groups_by_port_index():
    client = BrocadeSSHClient(host="10.10.11.11", username="admin", password="x")
    entries = client._parse_nsshow(NSSHOW_SAMPLE)

    from app.brocade.models import SwitchSnapshot, SwitchInfo
    snapshot = SwitchSnapshot(switch=SwitchInfo(name="tf1sw1", wwn="x"), ports=[], name_server=entries)
    grouped = snapshot.name_server_by_port_index()

    assert set(grouped.keys()) == {2, 8}
    assert len(grouped[2]) == 2   # physical + NPIV sibling
    assert len(grouped[8]) == 1


# Real `portshow 16` output. This is the regression test for a real bug:
# switchshow's trailing WWN column and portshow's "portWwn:" line looked
# similar enough to conflate, but they are DIFFERENT values -- the
# switch's own port WWN (portWwn:) vs the attached device's WWN
# (portWwn of device(s) connected:). Getting this wrong caused devices
# to falsely "self-match" the switch's own interface during cabling.
PORTSHOW_16_SAMPLE = """portIndex:  16
portName: port16
portHealth: Fabric vision license not present. Please install the license and retry the operation. 

Authentication: None
portDisableReason: None
portCFlags: 0x1
portFlags: 0x24b03\t PRESENT ACTIVE F_PORT G_PORT U_PORT LOGICAL_ONLINE LOGIN NOELP LED ACCEPT FLOGI
LocalSwcFlags: 0x0
portType:  17.0
POD Port: Port is licensed
portState: 1\tOnline   
Protocol: FC
portPhys:  6\tIn_Sync  \tportScn:   32\tF_Port    
port generation number:    130
state transition count:    5          

portId:    061000
portIfId:    43020027
portWwn:   20:10:00:05:33:88:72:30
portWwn of device(s) connected:
\t21:00:00:24:ff:47:b3:06
Distance:  normal
portSpeed: N8Gbps
"""


def test_apply_own_port_wwns_uses_portshow_not_switchshow():
    from unittest.mock import patch
    from app.brocade.models import PortInfo, PortState

    client = BrocadeSSHClient(host="10.10.11.11", username="admin", password="x")
    port = PortInfo(index=16, name="port16", state=PortState.ONLINE)

    with patch.object(BrocadeSSHClient, "_run", return_value=PORTSHOW_16_SAMPLE) as mock_run:
        client._apply_own_port_wwns([port])
        mock_run.assert_called_once_with("portshow 16")

    # must pick up the SWITCH's own WWN ("portWwn:") ...
    assert port.wwn == "20:10:00:05:33:88:72:30"
    # ... and must NOT pick up the attached DEVICE's WWN ("portWwn of
    # device(s) connected:"), even though both lines start with "portWwn"
    assert port.wwn != "21:00:00:24:ff:47:b3:06"


def test_apply_own_port_wwns_skips_offline_ports():
    from unittest.mock import patch
    from app.brocade.models import PortInfo, PortState

    client = BrocadeSSHClient(host="10.10.11.11", username="admin", password="x")
    offline_port = PortInfo(index=0, name="port0", state=PortState.OFFLINE)

    with patch.object(BrocadeSSHClient, "_run") as mock_run:
        client._apply_own_port_wwns([offline_port])
        mock_run.assert_not_called()
    assert offline_port.wwn is None


# Real ipaddrshow output for a fixed-port switch.
IPADDRSHOW_SAMPLE = """
SWITCH
Ethernet IP Address: 172.18.3.23
Ethernet Subnetmask: 255.255.255.0
Gateway IP Address: 172.18.3.1
DHCP: Off
"""


def test_parse_ipaddrshow():
    ip, prefix_len = BrocadeSSHClient._parse_ipaddrshow(IPADDRSHOW_SAMPLE)
    assert ip == "172.18.3.23"
    assert prefix_len == 24
