import json
from datetime import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from network.services import network_summary


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def ago(iso):
    if not iso:
        return "never"
    seconds = int((timezone.now() - datetime.fromisoformat(iso)).total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


class Command(BaseCommand):
    help = "Show the storage network: nodes, their copies, and how well files are replicated."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        summary = network_summary()
        if options["json"]:
            self.stdout.write(json.dumps(summary, indent=2))
            return

        out = self.stdout.write
        nodes = summary["nodes"]
        out(f"Nodes ({len(nodes)})")
        if not nodes:
            out("  none yet. Invite one with: manage.py invite_node --operator NAME")
        for n in nodes:
            state = "online" if n["online"] else "offline"
            if n["status"] != "active":
                state = f"{n['status']}: {n['status_reason']}"
            r = n["replicas"]
            copies = f"{r.get('stored', 0)} stored"
            for extra in ("assigned", "releasing", "lost"):
                if r.get(extra):
                    copies += f", {r[extra]} {extra}"
            out(
                f"  {n['name']:<16} {n['operator']:<18} {state:<9} seen {ago(n['last_seen_at']):<9} "
                f"{human(n['used_bytes'])} / {human(n['capacity_bytes'])}  {copies}"
            )

        b = summary["blobs"]
        target = summary["target_copies"]
        out("")
        out(f"Segments: {b['total']} live, {b['fully_replicated']} with {target}+ copies, "
            f"{b['under_replicated']} still spreading, {b['on_gateway']} still on the gateway, "
            f"{b['being_deleted']} being deleted")
        if summary["eligible_operators"] < target:
            out(
                f"\nWarning: only {summary['eligible_operators']} operator(s) have an online node. "
                f"Files can't reach {target} copies until at least {target} different operators are online."
            )
