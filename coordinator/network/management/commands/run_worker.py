import logging
import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from network.services import collect_gateway_garbage, run_pass

logger = logging.getLogger("network.worker")


class Command(BaseCommand):
    help = (
        "Keep the storage network in shape: place copies on nodes, retry stuck ones, release the "
        "gateway's copies once nodes hold them, finish deletions, and clear abandoned uploads."
    )

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
        parser.add_argument("--interval", type=float, default=settings.WORKER_INTERVAL_SECONDS)

    def handle(self, *args, **options):
        stop = {"now": False}

        def request_stop(*_):
            stop["now"] = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        last_gc = time.monotonic()
        logger.info("Worker started (every %.1fs)", options["interval"])
        while not stop["now"]:
            try:
                report = run_pass()
                if report.any():
                    logger.info("Pass: %s", vars(report))
                if time.monotonic() - last_gc >= settings.GATEWAY_GC_INTERVAL_SECONDS:
                    collect_gateway_garbage()
                    last_gc = time.monotonic()
            except Exception:  # noqa: BLE001 - one bad pass must not stop the worker
                logger.exception("Worker pass failed; retrying next interval")
            if options["once"]:
                break
            # Drop database connections that broke or aged out, so the next pass reconnects cleanly.
            close_old_connections()
            slept = 0.0
            while slept < options["interval"] and not stop["now"]:
                time.sleep(0.2)
                slept += 0.2
        logger.info("Worker stopped")
