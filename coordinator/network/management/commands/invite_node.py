import ipaddress
import json
import re
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from network.models import NodeInvite, Operator
from storage.kubo import KuboClient, KuboError

SWARM_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


def gateway_bootstrap_address():
    """The address nodes dial to join the private network: the gateway's IPFS node."""
    peer_id = KuboClient(settings.KUBO_API_URL, timeout=10).id()["ID"]
    host = settings.GATEWAY_P2P_HOST
    try:
        family = "ip6" if ipaddress.ip_address(host).version == 6 else "ip4"
    except ValueError:
        family = "dns4"
    return f"/{family}/{host}/tcp/{settings.GATEWAY_P2P_PORT}/p2p/{peer_id}"


class Command(BaseCommand):
    help = (
        "Create a single-use invite for a new storage node and print the settings the operator "
        "pastes into their node's .env file."
    )

    def add_arguments(self, parser):
        parser.add_argument("--operator", required=True, help="Operator name. Created if it doesn't exist yet.")
        parser.add_argument("--contact", default="", help="Operator phone or email (only used when creating).")
        parser.add_argument("--node-name", default="", help="Suggested name for the node, e.g. 'harare-1'.")
        parser.add_argument("--ttl-hours", type=int, default=settings.NODE_INVITE_TTL_HOURS)
        parser.add_argument("--json", action="store_true", help="Print JSON instead of .env lines.")

    def handle(self, *args, **options):
        swarm_key = settings.IPFS_SWARM_KEY.strip().lower()
        if not SWARM_KEY_RE.match(swarm_key):
            raise CommandError("IPFS_SWARM_KEY isn't set. Create one with `manage.py create_swarm_key`.")
        try:
            bootstrap = gateway_bootstrap_address()
        except (KuboError, KeyError) as exc:
            raise CommandError(f"Couldn't reach the gateway's IPFS node at {settings.KUBO_API_URL}: {exc}") from exc

        operator, created = Operator.objects.get_or_create(
            name=options["operator"].strip(), defaults={"contact": options["contact"]}
        )
        invite, code = NodeInvite.issue(operator, options["node_name"], ttl=timedelta(hours=options["ttl_hours"]))

        values = {
            "COORDINATOR_URL": settings.PUBLIC_URL,
            "ENROLLMENT_CODE": code,
            "NODE_NAME": options["node_name"],
            "IPFS_SWARM_KEY": swarm_key,
            "GATEWAY_BOOTSTRAP": bootstrap,
        }
        if options["json"]:
            self.stdout.write(json.dumps({**values, "operator": operator.name, "operator_created": created}))
            return

        if created:
            self.stderr.write(f"Created operator '{operator.name}'.")
        self.stderr.write(
            f"Invite for {operator.name}, valid until {invite.expires_at:%d %b %Y %H:%M} UTC and usable once.\n"
            "Send these lines to the operator to paste into node/.env (this is the only time the code is shown):\n"
        )
        for key, value in values.items():
            self.stdout.write(f"{key}={value}")
