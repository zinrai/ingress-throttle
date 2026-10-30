#!/usr/bin/env python3
"""Temporarily throttle inbound bandwidth on a network interface.

Inbound packets on the interface are redirected to a dedicated IFB device,
where a cake qdisc shapes them to the given rate. cake handles flows fairly,
so small flows are not stuck behind bulk transfers.
"""

import argparse
import json
import shlex
import subprocess
import sys

# Not 49152, tc's default: a filter added without a pref gets 49152, or one
# below the lowest pref in use, so it could never land on 65535.
FILTER_PREF = "65535"
FILTER_HANDLE = "1"
IFB = "ifbthrottle0"


def fail(args, proc):
    msg = proc.stderr.strip() or f"exit status {proc.returncode}"
    sys.exit(f"error: {shlex.join(args)}: {msg}")


class Runner:
    def __init__(self, dry_run):
        self.dry_run = dry_run

    def change(self, args):
        print(f"+ {shlex.join(args)}")
        if self.dry_run:
            return
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            fail(args, proc)

    @staticmethod
    def show(args):
        print(f"$ {shlex.join(args)}")
        proc = subprocess.run(args, capture_output=True, text=True)
        print((proc.stdout or proc.stderr).rstrip() or "(empty)")
        print()

    @staticmethod
    def read_json(args):
        # Not read as empty on failure: "off" would then skip deleting the
        # redirect filter and leave it pointing at a deleted IFB device.
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode != 0:
            fail(args, proc)
        return json.loads(proc.stdout or "[]")


def link_kind(name):
    """Return the link type of a device, "" if it has none, None if it is absent."""
    # All links are listed, as "show dev" fails both for an absent device and
    # for any other error.
    for link in Runner.read_json(["ip", "-j", "-d", "link", "show"]):
        if link.get("ifname") == name:
            return link.get("linkinfo", {}).get("info_kind", "")
    return None


def qdisc_kinds(interface):
    qdiscs = Runner.read_json(["tc", "-j", "qdisc", "show", "dev", interface])
    return {q.get("kind") for q in qdiscs}


def ingress_filter_prefs(interface):
    filters = Runner.read_json(
        ["tc", "-j", "filter", "show", "dev", interface, "ingress"]
    )
    return {str(f.get("pref")) for f in filters}


def cmd_on(args, run):
    if link_kind(args.interface) is None:
        sys.exit(f"error: {args.interface} does not exist")

    # Without numifbs=0, loading the module creates ifb0 and ifb1, which
    # nothing would remove.
    run.change(["modprobe", "ifb", "numifbs=0"])
    kind = link_kind(IFB)
    if kind is None:
        run.change(["ip", "link", "add", "name", IFB, "type", "ifb"])
    elif kind != "ifb":
        sys.exit(f"error: {IFB} exists and is not an IFB device")
    run.change(["ip", "link", "set", "dev", IFB, "up"])

    # Not redirected first: until cake is in place, traffic would pass the IFB
    # device unshaped.
    run.change(
        [
            "tc",
            "qdisc",
            "replace",
            "dev",
            IFB,
            "root",
            "cake",
            "bandwidth",
            args.rate,
            "besteffort",
            "ingress",
        ]
    )

    # An existing ingress or clsact qdisc provides the ingress hook, so it is
    # used as is; replacing a clsact qdisc would drop its filters.
    if not qdisc_kinds(args.interface) & {"ingress", "clsact"}:
        run.change(
            ["tc", "qdisc", "add", "dev", args.interface, "handle", "ffff:", "ingress"]
        )

    # flower with no match keys matches every packet. Not matchall: it refuses
    # to replace an existing filter (File exists), so "on" could not run twice.
    run.change(
        [
            "tc",
            "filter",
            "replace",
            "dev",
            args.interface,
            "ingress",
            "pref",
            FILTER_PREF,
            "handle",
            FILTER_HANDLE,
            "protocol",
            "all",
            "flower",
            "action",
            "mirred",
            "egress",
            "redirect",
            "dev",
            IFB,
        ]
    )


def remove_redirect(interface, run):
    prefs = ingress_filter_prefs(interface)
    # Deleted by pref rather than flushing ingress, so other filters stay.
    if FILTER_PREF in prefs:
        run.change(
            ["tc", "filter", "del", "dev", interface, "ingress", "pref", FILTER_PREF]
        )

    # Remove the ingress qdisc only if it is a plain "ingress" qdisc with no
    # filters left. A clsact qdisc, or one still holding other filters,
    # belongs to someone else.
    if prefs - {FILTER_PREF}:
        print(f"keeping ingress qdisc on {interface}: other filters remain")
    elif "ingress" in qdisc_kinds(interface):
        run.change(["tc", "qdisc", "del", "dev", interface, "ingress"])


def cmd_off(args, run):
    # Stop redirecting first, so traffic is never sent to a missing device.
    if link_kind(args.interface) is None:
        print(f"{args.interface} is gone: nothing to remove there")
    else:
        remove_redirect(args.interface, run)

    kind = link_kind(IFB)
    if kind == "ifb":
        # Deleting the IFB device also removes its qdisc.
        run.change(["ip", "link", "del", "dev", IFB])
    elif kind is not None:
        print(f"keeping {IFB}: not an IFB device")


def cmd_status(args, run):
    Runner.show(["tc", "qdisc", "show", "dev", args.interface, "ingress"])
    Runner.show(["tc", "filter", "show", "dev", args.interface, "ingress"])
    Runner.show(["ip", "link", "show", "dev", IFB])
    Runner.show(["tc", "-s", "qdisc", "show", "dev", IFB])


def main():
    parser = argparse.ArgumentParser(
        description="Temporarily throttle inbound bandwidth on a network interface.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text in (
        ("on", cmd_on, "apply or update the inbound throttle"),
        ("off", cmd_off, "remove the inbound throttle"),
        ("status", cmd_status, "show the current state"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "-i", "--interface", required=True, help="interface to throttle (e.g. eth0)"
        )
        if name == "on":
            p.add_argument(
                "-r", "--rate", required=True, help="rate in tc syntax (e.g. 300mbit)"
            )
        if name != "status":
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="print commands without running them",
            )
        p.set_defaults(func=func)

    args = parser.parse_args()
    args.func(args, Runner(getattr(args, "dry_run", False)))


if __name__ == "__main__":
    main()
