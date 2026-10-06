import secrets

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate the shared key for the private IPFS network. Put it in .env as IPFS_SWARM_KEY."

    def handle(self, *args, **options):
        key = secrets.token_hex(32)
        self.stdout.write(f"IPFS_SWARM_KEY={key}")
        self.stderr.write(
            "\nAdd that line to the coordinator's .env. Every node gets the same key in its invite.\n"
            "Anyone with this key can join the IPFS network (but can't read any files: they're encrypted).\n"
            "Changing it later means re-sending invites to every node."
        )
