"""
Thin wrapper around pynetbox covering everything the sync needs --
**core NetBox only** (dcim.Manufacturer, DeviceType, DeviceRole, Site,
Device, Interface, Cable, plus extras.Tag). No plugin required: the
WWN itself is stored on NetBox's own built-in `dcim.Interface.wwn`
field, which has shipped in core NetBox for years specifically for
Fibre Channel use cases like this one. NPIV parent/child relationships
use core NetBox's own `Interface.parent` field the same way you'd model
a sub-interface or virtual NIC.

Every object this tool creates gets `managed_tag` applied, so you can
always find (or bulk clean up) exactly what came from this sync and
never touch anything you built by hand. If a switch config sets
`fabric: <name>`, that's applied as a second, plain NetBox Tag on the
switch's Device (e.g. tag "fabric-Fabric-Test-A") purely for filtering/
grouping in the UI -- it's optional and has no special meaning to
NetBox itself, so it works on any NetBox install with zero plugins.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import pynetbox
import urllib3

from app.brocade.models import PortInfo, SwitchInfo
from app.config import AppConfig, SwitchConfig
from app.netbox.interface_types import kbps_for_speed, slug_for_speed

log = logging.getLogger(__name__)

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9_-]+")
_SLUG_REPEATED_HYPHENS = re.compile(r"-{2,}")


def _slugify(name: str, max_length: int = 100) -> str:
    """NetBox slug fields only accept letters, numbers, underscores and
    hyphens -- anything else (dots, colons, plus signs, parentheses...)
    gets a 400 Bad Request. This name has already broken once on a
    firmware version string like "Brocade Fabric OS 7.4.2c" (the dots),
    so this is regex-based rather than replacing a couple of characters
    by hand, to actually cover the general case instead of the next one
    we happen to notice."""
    slug = name.strip().lower().replace(" ", "-").replace("/", "-")
    slug = _SLUG_INVALID_CHARS.sub("-", slug)
    slug = _SLUG_REPEATED_HYPHENS.sub("-", slug).strip("-")
    return slug[:max_length] or "unnamed"


class NetBoxSyncClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.dry_run = config.sync.dry_run
        self.nb = pynetbox.api(config.netbox.url, token=config.netbox.token)
        # `verify_tls` can be `true` (verify against system CAs -- fails
        # on a self-signed cert), `false` (skip verification entirely --
        # fine for internal-only instances, but noisy: every request
        # emits an InsecureRequestWarning), or a *string path* to a CA
        # bundle file (proper verification against your internal CA,
        # e.g. if you mount it into the container and point here) --
        # `requests` (which pynetbox sits on top of) accepts all three
        # natively, so this is just passed straight through.
        self.nb.http_session.verify = config.netbox.verify_tls
        if config.netbox.verify_tls is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # small in-process caches to cut down on repeated lookups within one run
        # (must be initialized before _ensure_tag() below, which uses _tag_cache)
        self._manufacturer_cache: dict[str, object] = {}
        self._device_type_cache: dict[tuple[str, str], object] = {}
        self._role_cache: dict[str, object] = {}
        self._site_cache: dict[str, object] = {}
        self._tag_cache: dict[str, object] = {}

        self._managed_tag = self._ensure_tag(config.netbox.managed_tag)

    # -- tags -----------------------------------------------------------------

    def _ensure_tag(self, name: str):
        if name in self._tag_cache:
            return self._tag_cache[name]
        tag = self.nb.extras.tags.get(name=name)
        if not tag:
            if self.dry_run:
                log.info("[dry-run] would create tag '%s'", name)
                self._tag_cache[name] = None
                return None
            tag = self.nb.extras.tags.create(name=name, slug=_slugify(name))
        self._tag_cache[name] = tag
        return tag

    def _tags(self, extra_tag_name: Optional[str] = None) -> list[dict]:
        tags = [{"id": self._managed_tag.id}] if self._managed_tag else []
        if extra_tag_name:
            extra = self._ensure_tag(extra_tag_name)
            if extra:
                tags.append({"id": extra.id})
        return tags

    # -- core dcim helpers -----------------------------------------------------

    def get_or_create_manufacturer(self, name: str):
        if name in self._manufacturer_cache:
            return self._manufacturer_cache[name]
        obj = self.nb.dcim.manufacturers.get(name=name)
        if not obj:
            if self.dry_run:
                log.info("[dry-run] would create manufacturer '%s'", name)
            else:
                obj = self.nb.dcim.manufacturers.create(name=name, slug=_slugify(name))
        self._manufacturer_cache[name] = obj
        return obj

    def get_or_create_device_type(self, manufacturer_name: str, model: str):
        key = (manufacturer_name, model)
        if key in self._device_type_cache:
            return self._device_type_cache[key]
        manufacturer = self.get_or_create_manufacturer(manufacturer_name)
        obj = self.nb.dcim.device_types.get(model=model, manufacturer_id=getattr(manufacturer, "id", None))
        if not obj:
            if self.dry_run:
                log.info("[dry-run] would create device type '%s / %s'", manufacturer_name, model)
            else:
                obj = self.nb.dcim.device_types.create(
                    manufacturer=manufacturer.id,
                    model=model,
                    slug=_slugify(model),
                )
        self._device_type_cache[key] = obj
        return obj

    def get_or_create_device_role(self, name: str):
        if name in self._role_cache:
            return self._role_cache[name]
        obj = self.nb.dcim.device_roles.get(name=name)
        if not obj:
            if self.dry_run:
                log.info("[dry-run] would create device role '%s'", name)
            else:
                obj = self.nb.dcim.device_roles.create(name=name, slug=_slugify(name), color="2196f3")
        self._role_cache[name] = obj
        return obj

    def get_or_create_site(self, name: str):
        if name in self._site_cache:
            return self._site_cache[name]
        obj = self.nb.dcim.sites.get(name=name) or self.nb.dcim.sites.get(slug=_slugify(name))
        if not obj:
            if self.dry_run:
                log.info("[dry-run] would create site '%s'", name)
            else:
                obj = self.nb.dcim.sites.create(name=name, slug=_slugify(name), status="active")
        self._site_cache[name] = obj
        return obj

    def get_or_create_platform(self, name: str, manufacturer_name: str = "Brocade"):
        obj = self.nb.dcim.platforms.get(name=name)
        if not obj:
            manufacturer = self.get_or_create_manufacturer(manufacturer_name)
            if self.dry_run:
                log.info("[dry-run] would create platform '%s'", name)
                return None
            obj = self.nb.dcim.platforms.create(
                name=name, slug=_slugify(name), manufacturer=getattr(manufacturer, "id", None)
            )
        return obj

    def get_or_create_switch_device(self, switch_cfg: SwitchConfig, switch_info: SwitchInfo):
        device = self.nb.dcim.devices.get(name=switch_cfg.name)
        model = switch_info.model or "Unknown Brocade Switch"
        device_type = self.get_or_create_device_type("Brocade", model)
        role = self.get_or_create_device_role(switch_cfg.device_role)
        site = self.get_or_create_site(switch_cfg.site) if switch_cfg.site else None
        platform = (
            self.get_or_create_platform(f"Brocade Fabric OS {switch_info.firmware}")
            if switch_info.firmware
            else None
        )

        payload = dict(
            name=switch_cfg.name,
            device_type=getattr(device_type, "id", None),
            role=getattr(role, "id", None),
            site=getattr(site, "id", None),
            platform=getattr(platform, "id", None),
            serial=switch_info.serial_number or "",
            status="active",
            tags=self._tags(extra_tag_name=switch_cfg.fabric_tag),
        )
        if device:
            if not self.dry_run:
                for k, v in payload.items():
                    if v is not None:
                        setattr(device, k, v)
                device.save()
            return device

        if self.dry_run:
            log.info("[dry-run] would create device '%s' (%s)", switch_cfg.name, model)
            return None
        return self.nb.dcim.devices.create(**{k: v for k, v in payload.items() if v is not None})

    def assign_primary_ip(self, device, ip: str, prefix_len: Optional[int]) -> None:
        """Ensures a management Interface exists on the device, assigns
        an IPAddress to it, and sets it as the device's primary IPv4.
        `prefix_len` defaults to /32 if the subnet mask couldn't be
        determined (still usable as an identifying address, just without
        implying a specific subnet)."""
        if device is None or not ip:
            return
        prefix_len = prefix_len or 32

        iface_name = "mgmt0"
        iface = self.nb.dcim.interfaces.get(device_id=device.id, name=iface_name)
        if not iface:
            if self.dry_run:
                log.info("[dry-run] would create management interface '%s' on '%s'", iface_name, device.name)
                return
            iface = self.nb.dcim.interfaces.create(
                device=device.id,
                name=iface_name,
                type="1000base-t",
                mgmt_only=True,
                enabled=True,
                tags=self._tags(),
            )

        address = f"{ip}/{prefix_len}"
        existing = self.nb.ipam.ip_addresses.get(address=address)
        if existing:
            ip_obj = existing
            if not self.dry_run and (
                getattr(existing, "assigned_object_id", None) != iface.id
                or getattr(existing, "assigned_object_type", None) != "dcim.interface"
            ):
                existing.assigned_object_type = "dcim.interface"
                existing.assigned_object_id = iface.id
                existing.save()
        else:
            if self.dry_run:
                log.info("[dry-run] would create IP address %s on '%s'", address, iface_name)
                return
            ip_obj = self.nb.ipam.ip_addresses.create(
                address=address,
                assigned_object_type="dcim.interface",
                assigned_object_id=iface.id,
                status="active",
                tags=self._tags(),
            )

        if self.dry_run:
            return
        if getattr(device, "primary_ip4", None) is None or device.primary_ip4.id != ip_obj.id:
            device.primary_ip4 = ip_obj.id
            device.save()

    # -- interfaces / WWN / cabling (all core dcim, no plugin) -----------------

    def find_interface_by_wwn(self, wwn: str):
        results = list(self.nb.dcim.interfaces.filter(wwn=wwn))
        if not results:
            return None
        if len(results) > 1:
            log.warning("multiple NetBox interfaces share WWN %s (using first: id=%s)", wwn, results[0].id)
        return results[0]

    # -- interface type validation (NetBox's exact set of valid FC type
    #    slugs can differ by version/deployment -- rather than trusting a
    #    hardcoded guess, ask NetBox itself what it accepts) -------------

    def _valid_interface_types(self) -> Optional[set[str]]:
        """Queries NetBox's own API (via an HTTP OPTIONS request, standard
        DRF ChoiceField metadata) for the exact set of valid
        `Interface.type` slugs on this specific instance. Cached for the
        life of the run. Returns None (meaning "skip validation, trust
        our own guess") if the request fails for any reason -- some
        NetBox setups restrict OPTIONS, and this must never be the thing
        that breaks a sync."""
        if hasattr(self, "_interface_type_cache"):
            return self._interface_type_cache

        self._interface_type_cache = None
        try:
            url = f"{self.config.netbox.url.rstrip('/')}/api/dcim/interfaces/"
            resp = self.nb.http_session.options(url, timeout=10)
            resp.raise_for_status()
            choices = resp.json()["actions"]["POST"]["type"]["choices"]
            self._interface_type_cache = {c["value"] for c in choices}
            log.debug("NetBox reports %d valid interface types", len(self._interface_type_cache))
        except Exception as exc:
            log.debug("could not fetch valid interface types from NetBox (skipping validation): %s", exc)
        return self._interface_type_cache

    def _safe_interface_type(self, guessed_slug: str) -> str:
        valid = self._valid_interface_types()
        if valid is None or guessed_slug in valid:
            return guessed_slug
        log.warning(
            "NetBox rejected/doesn't offer interface type '%s' on this instance -- "
            "falling back to 'other'. This usually means a NetBox version difference; "
            "if you'd rather it use a specific type, check `curl -X OPTIONS %s/api/dcim/interfaces/` "
            "for the exact slugs this instance accepts.",
            guessed_slug, self.config.netbox.url.rstrip("/"),
        )
        return "other"

    def get_or_create_port_interface(self, device, port: PortInfo, switch_cfg: SwitchConfig, parent_interface=None):
        """Creates/updates the physical switch port as a core dcim.Interface,
        including its own WWN (core NetBox `Interface.wwn` field -- no
        plugin needed), negotiated speed/type, and enabled state.
        """
        name = port.name
        iface = self.nb.dcim.interfaces.get(device_id=device.id, name=name) if device else None

        payload = dict(
            device=getattr(device, "id", None),
            name=name,
            type=self._safe_interface_type(slug_for_speed(port.speed_gbps or port.max_speed_gbps)),
            enabled=port.enabled,
            wwn=port.wwn or "",
            speed=kbps_for_speed(port.speed_gbps),
            description=port.description or port.attachment_note or "",
            tags=self._tags(extra_tag_name=switch_cfg.fabric_tag),
        )
        if parent_interface is not None:
            payload["parent"] = parent_interface.id

        if iface:
            if not self.dry_run:
                for k, v in payload.items():
                    if v is not None:
                        setattr(iface, k, v)
                iface.save()
            return iface

        if self.dry_run:
            log.info("[dry-run] would create interface '%s' on device '%s'", name, getattr(device, "name", "?"))
            return None
        return self.nb.dcim.interfaces.create(**{k: v for k, v in payload.items() if v is not None})

    def ensure_cable(self, interface_a, interface_b) -> str:
        """Creates a Cable between interface_a (the switch port) and
        interface_b (the resolved far-end device interface) if neither
        end is already cabled. Returns a short status string so the
        caller can log something more useful than a single opaque
        counter: "created", "already_connected" (interface_a already had
        a cable), "target_busy" (interface_b already had a *different*
        cable -- common when a WWN is duplicated across more than one
        NetBox interface, see find_interface_by_wwn's warning), "dry_run",
        or "skipped" (missing/identical interfaces).
        """
        if interface_a is None or interface_b is None:
            log.warning("ensure_cable called with a missing interface -- skipping (a=%s, b=%s)", interface_a, interface_b)
            return "skipped"
        if interface_a.id == interface_b.id:
            log.warning(
                "switch port '%s' and its resolved cable target are the SAME NetBox interface "
                "(id=%s) -- this means a device WWN incorrectly matched the switch's own port "
                "WWN in NetBox (often caused by a duplicate/stale WWN entry). Skipping this cable; "
                "the underlying duplicate WWN in NetBox needs cleaning up for this to resolve correctly.",
                interface_a.name, interface_a.id,
            )
            return "self_match_skipped"

        # refresh both ends to get current connection state -- checking
        # only interface_a (the old behavior) missed the case where the
        # *target* interface was already cabled to something else, which
        # NetBox would then reject outright when we tried to create a
        # second cable on it.
        interface_a = self.nb.dcim.interfaces.get(interface_a.id)
        interface_b = self.nb.dcim.interfaces.get(interface_b.id)

        device_a = getattr(interface_a, "device", None)
        device_b = getattr(interface_b, "device", None)
        device_a_id = device_a["id"] if isinstance(device_a, dict) else getattr(device_a, "id", None)
        device_b_id = device_b["id"] if isinstance(device_b, dict) else getattr(device_b, "id", None)
        if device_a_id is not None and device_a_id == device_b_id:
            # A device WWN resolved to a *different* interface on the
            # SAME switch device (not literally the same interface --
            # that's the id-equality check above -- but still clearly
            # wrong: cabling a switch port to another port on itself).
            # Same root cause as the exact self-match case: a duplicate/
            # incorrect WWN entry in NetBox that happens to match one of
            # this switch's own port WWNs.
            log.warning(
                "switch port '%s' resolved a cable target ('%s') that belongs to the SAME switch "
                "device, not a real far-end device -- skipping. This means some WWN logged into "
                "this port matches another interface's WWN on this same switch in NetBox (a "
                "duplicate/incorrect WWN entry needs cleaning up there).",
                interface_a.name, interface_b.name,
            )
            return "same_device_skipped"

        if interface_a.cable:
            log.debug("interface %s already cabled -- leaving as-is", interface_a.name)
            return "already_connected"
        if interface_b.cable:
            log.warning(
                "interface %s resolved as the cable target for %s, but %s is already cabled to "
                "something else -- skipping (likely a duplicate-WWN interface in NetBox; see the "
                "'multiple NetBox interfaces share WWN' warning above for this WWN)",
                interface_b.name, interface_a.name, interface_b.name,
            )
            return "target_busy"

        if self.dry_run:
            log.info(
                "[dry-run] would create cable %s <-> %s",
                getattr(interface_a, "display", interface_a.name),
                getattr(interface_b, "display", interface_b.name),
            )
            return "dry_run"

        try:
            self.nb.dcim.cables.create(
                a_terminations=[{"object_type": "dcim.interface", "object_id": interface_a.id}],
                b_terminations=[{"object_type": "dcim.interface", "object_id": interface_b.id}],
                status="connected",
                tags=self._tags(),
            )
        except pynetbox.RequestError as exc:
            # Never let one bad cable abort the rest of this switch's
            # ports -- log it clearly and keep going.
            log.warning("failed to create cable %s <-> %s: %s", interface_a.name, interface_b.name, exc)
            return "error"
        return "created"
