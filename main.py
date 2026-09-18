#!/usr/bin/env python3
"""
brocade-netbox-san-sync -- entrypoint.

Examples:
    python main.py --config config.yaml                 # one sync pass, exit
    python main.py --config config.yaml --dry-run        # log what would change, touch nothing
    python main.py --config config.yaml --interval 900   # loop forever, sync every 15 min (for Docker)
    python main.py --config config.yaml --switch SW01    # only sync one switch from the file
    python main.py --config config.yaml --dump-raw switchshow --switch SW01
        # SSH switches: prints the raw CLI output of any command, unparsed --
        # use this first against a new switch/firmware to check the parser
        # regexes in app/brocade/ssh_client.py actually match your output.
    python main.py --config config.yaml --dump-raw brocade-interface/fibrechannel --switch SW02
        # REST switches: prints the raw JSON of any /rest/running/<resource>.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace

from app.brocade.factory import build_client
from app.config import load_config
from app.logging_setup import setup_logging
from app.sync.orchestrator import run_sync

log = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync Brocade SAN switches into NetBox (core dcim objects, no plugin required)")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    p.add_argument("--dry-run", action="store_true", help="log intended changes without writing to NetBox")
    p.add_argument("--interval", type=int, default=0, help="seconds between sync passes; omit to run once and exit")
    p.add_argument("--switch", action="append", help="only sync this switch name from config.yaml (repeatable)")
    p.add_argument("--dump-raw", metavar="COMMAND_OR_RESOURCE", help="print raw switch output/JSON and exit (debug)")
    p.add_argument("--log-level", default=None, help="override log_level from config.yaml")
    return p.parse_args()


def do_dump_raw(config, target_switch: str, command_or_resource: str) -> int:
    switches = [s for s in config.switches if s.name == target_switch]
    if not switches:
        log.error("no switch named '%s' in config", target_switch)
        return 1
    switch_cfg = switches[0]
    client = build_client(switch_cfg)
    with client:
        if switch_cfg.method == "ssh":
            print(client.dump_raw(command_or_resource))
        else:
            print(json.dumps(client.dump_raw(command_or_resource), indent=2))
    return 0


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    if args.log_level:
        config.log_level = args.log_level
    setup_logging(config.log_level)

    if args.dry_run:
        config.sync = replace(config.sync, dry_run=True)

    if args.switch:
        config.switches = [s for s in config.switches if s.name in args.switch]
        if not config.switches:
            log.error("--switch filter matched no switches in config")
            return 1

    if args.dump_raw:
        if not args.switch or len(args.switch) != 1:
            log.error("--dump-raw requires exactly one --switch NAME")
            return 1
        return do_dump_raw(config, args.switch[0], args.dump_raw)

    if args.interval and args.interval > 0:
        log.info("running in loop mode, interval=%ss (Ctrl+C / SIGTERM to stop)", args.interval)
        while True:
            failures = run_sync(config)
            if failures:
                log.warning("sync pass completed with %d switch failure(s)", failures)
            else:
                log.info("sync pass completed cleanly")
            time.sleep(args.interval)
    else:
        failures = run_sync(config)
        if failures:
            log.warning("sync completed with %d switch failure(s)", failures)
            return 1
        log.info("sync completed cleanly")
        return 0


if __name__ == "__main__":
    sys.exit(main())
