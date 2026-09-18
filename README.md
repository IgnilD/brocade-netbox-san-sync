# brocade-netbox-san-sync

Syncs Brocade FC switches into **stock NetBox** -- no plugin required.
In the spirit of [netbox-sync](https://github.com/bb-ricardo/netbox-sync):
point it at your switches and your NetBox instance, and it keeps
interfaces, WWNs, and physical cabling up to date automatically, using
only core NetBox objects (`dcim.Device`, `dcim.Interface` -- including
its built-in `wwn` field, `dcim.Cable`, `extras.Tag`). Works against any
NetBox 4.x instance as-is.

Supports both connection methods to the switch, chosen per-switch in
config:
- **SSH** -- works on every FOS version, including old ones. Required
  for FOS < 8.2.1 (Brocade's REST API doesn't exist before that).
- **REST** -- FOS 8.2.1+ only. Faster and returns structured JSON
  instead of screen-scraped text.

## What it does, per switch, each run

1. **Switch identity** -- connects (SSH or REST), reads model, serial,
   firmware, domain ID, and management IP; creates/updates the chassis
   as a NetBox `dcim.Device`:
   - **Model** is resolved from switchshow's `switchType`
     against Brocade's own published switchType-to-product-name table
     (not the raw part number, which is accurate but not human-friendly`).
     Falls back to the raw
     `chassisshow` part number for any switchType not in the table, so
     an unrecognized/very new switch still gets a sensible device type.
   - **Serial number** comes from `chassisshow`'s `Serial Num` field,
     written to the Device's own `serial` field (not the Device Type --
     serial is per-unit, so it won't appear on the Device Type page).
   - **Platform** is created/assigned as `Brocade Fabric OS <version>`
     (e.g. "Brocade Fabric OS 7.4.2c"), from `version`'s `Fabric OS:` line.
   - **Primary IPv4** comes from `ipaddrshow`'s `Ethernet IP Address` /
     `Ethernet Subnetmask`, assigned to a `mgmt0` management-only
     Interface and set as the Device's primary IPv4. On a director-class
     chassis (separate CP0/CP1), this takes whichever section
     `ipaddrshow` lists first, not necessarily the active CP -- fine for
     fixed-port switches, worth double-checking on directors.
2. **Interfaces** -- creates/updates one `dcim.Interface` per physical
   port: name, negotiated speed (mapped to NetBox's built-in Fibre
   Channel interface types -- `8gfc-sfpp`, `16gfc-sfpp`, `32gfc-sfp28`,
   etc. -- auto-adjusted at runtime to whatever slugs your specific
   NetBox instance actually accepts), enabled state, and **the port's
   own WWN**, fetched via `portshow <index>` and written to
   `Interface.wwn` (core NetBox's native field, no plugin). This is
   deliberately *not* read from `switchshow`'s trailing column, which is
   the attached device's info, not the switch port's own -- conflating
   the two previously caused devices to falsely match the switch's own
   interface as their cable target.
3. **Connections** -- reads the fabric name server (who's logged in,
   and through which local port) and, for every device WWN it finds,
   looks it up in NetBox. If found, it creates a `dcim.Cable` between
   the switch port and that device's interface -- **only if the WWN
   already exists in NetBox**. This tool verifies and links; it never
   invents a host or array that isn't already modeled.
4. **NPIV handling** -- a switch F-port can have several WWNs logged in
   at once (one physical HBA + N virtual/NPIV WWPNs). Rather than
   guessing, the name server (`nsshow -t` / REST `fibrechannel-name-server`)
   is joined to the switch's own port list by **Port Index** (not by
   any WWN column in `switchshow`, which is only populated when exactly
   one device is logged in -- with NPIV, Brocade prints a free-text
   summary like `1 N Port + 1 NPIV public` there instead). For each
   name-server entry, Brocade directly reports a `Device type`
   (`Physical Initiator+Target`, `NPIV Target`, `Physical Initiator`,
   ...) and, for NPIV entries, a `Permanent Port Name` pointing straight
   at the physical WWN it rides on -- so the physical/virtual split is
   read off the switch, not inferred. The NPIV sibling and its physical
   parent resolve to the *same* target WWN automatically and collapse
   into one cable, no ambiguity. If that WWN is found in NetBox, its
   `Interface.parent` is also checked -- core NetBox's own field for
   modeling a virtual WWPN as a child of its physical HBA interface --
   as a second safety net. Only if a port's entries genuinely disagree
   on the physical WWN (normally meaning older firmware that doesn't
   report `Device type` at all) does `ambiguous_wwn_strategy` kick in:
   `first` (default) cables to whichever WWN was listed first; `skip`
   leaves it uncabled and logs a warning instead.
5. **Grouping (optional)** -- if a switch's config sets `fabric_tag:
   <name>`, that name is applied as a plain NetBox Tag on the switch's
   Device and Interfaces (e.g. `Fabric-Test-A`). It's just a Tag, with
   no special meaning to NetBox -- purely so you can filter/group by
   fabric in the UI without needing any custom object type.

## What it deliberately does NOT do

- **It doesn't create hosts, arrays, or their virtual (NPIV) interfaces.**
  If a device WWN logged into the fabric isn't in NetBox yet, it's
  logged and skipped, not fabricated. Onboarding hosts/arrays (and
  their own NPIV child interfaces, if you use them) is a separate
  concern, handled however you already do it in NetBox.
- **It doesn't do zoning.** Physical connectivity and zoning intent are
  different problems; parsing `zoneshow`/`cfgshow` into whatever object
  model you use for zoning (a custom field, a plugin, or just
  descriptions/tags) is a reasonable next project built on top of this
  one, using the same Brocade clients.
- **It doesn't require, assume, or integrate with any specific NetBox
  plugin.** Everything it writes is plain core `dcim`/`extras` --
  portable to any NetBox 4.x install, and safe to run whether or not
  you later decide to layer a SAN-specific plugin on top for things
  like zoning or fabric objects.

## Setup

```bash
cp config.example.yaml config.yaml   # fill in real values directly -- switches, NetBox URL/token
pip install -r requirements.txt
python main.py --config config.yaml --dry-run   # see what it WOULD do first
python main.py --config config.yaml              # do it for real, once
```

### Docker

```bash
docker compose build
docker compose run --rm brocade-netbox-san-sync --config /app/config/config.yaml --dry-run
docker compose up -d   # runs continuously, --interval 900 (every 15 min) by default
```

`config.yaml` is bind-mounted into the container read-only -- it's the
single source of truth for everything (NetBox URL/token, switch
credentials, sync options). Nothing else to configure in
`docker-compose.yml`.

## Before your first real run against a new switch

CLI/REST output layout drifts slightly between FOS builds. Rather than
guessing, check what your switch actually returns:

```bash
# SSH switches -- prints raw, unparsed CLI output
python main.py --config config.yaml --switch SW-TEST-01 --dump-raw switchshow
python main.py --config config.yaml --switch SW-TEST-01 --dump-raw "sfpshow -all"
python main.py --config config.yaml --switch SW-TEST-01 --dump-raw "nsshow -t"

# REST switches -- prints raw JSON for any /rest/running/<resource>
python main.py --config config.yaml --switch SW-PROD-01 --dump-raw brocade-interface/fibrechannel
python main.py --config config.yaml --switch SW-PROD-01 --dump-raw brocade-name-server/fibrechannel-name-server
```

If a field comes back empty that you expected to see populated, that's
almost always a field-name or regex mismatch for your specific firmware
-- fix it in `app/brocade/ssh_client.py` or `app/brocade/rest_client.py`
and add a matching case to `tests/test_ssh_parsers.py` so it stays fixed.
The current SSH parsers and their tests are already grounded in real
`switchshow`/`nsshow -t` output from an actual switch (a Brocade
6510-class unit, FOS-era switchType 66.1), not invented samples -- the
REST path is the one part still resting on documented-but-unverified
field names, since it hasn't been run against a live 8.2.1+ switch yet.

## Project layout

```
app/
  config.py                  # YAML + env-var config loading
  brocade/
    models.py                # normalized dataclasses (backend-agnostic)
    base.py                  # abstract BrocadeClient interface
    ssh_client.py             # SSH backend (any FOS version)
    rest_client.py            # REST backend (FOS 8.2.1+)
    factory.py                 # picks ssh/rest per-switch from config
  netbox/
    client.py                 # pynetbox wrapper -- core dcim/extras only, no plugin
    interface_types.py        # speed -> NetBox FC interface type slug
  sync/
    switch_sync.py            # chassis Device (+ optional fabric_tag)
    interface_sync.py         # per-port dcim.Interface (incl. own WWN)
    connection_sync.py        # name-server WWN matching + Cable creation
    orchestrator.py           # runs the above across every switch
  utils/wwn.py                # WWN string normalization
main.py                       # CLI: --dry-run / --interval / --dump-raw / --switch
config.example.yaml
Dockerfile / docker-compose.yml / .env.example
tests/test_ssh_parsers.py     # parser regression tests, run with `pytest`
```

## Requirements

- NetBox 4.0+ -- core install, no plugin required
- Python 3.10+ (3.12 in the provided Docker image)
- A NetBox API token with write access to `dcim` and `extras.tags`
- Switch-side: a read-capable SSH/REST account is enough (this tool never
  issues config-changing commands to the switch, only read commands)
