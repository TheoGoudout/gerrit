# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .config import Config, ConfigError
from .ledger import Ledger
from .reconcile import Reconciler


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _serve(config, reconciler) -> int:
    """Run the webhook receiver and the sweep loop together.

    The sweep is not optional. Gerrit's webhooks plugin drops events after a
    few failed retries, so the receiver only shortens latency; the sweep is
    what makes the archive correct.
    """
    from .webhook import WebhookServer

    if not config.webhook.enabled:
        print(
            "webhook.enabled is false; use 'run' for sweep-only operation",
            file=sys.stderr,
        )
        return 2

    server = WebhookServer(
        host=config.webhook.host,
        port=config.webhook.port,
        path=config.webhook.path,
        token=config.webhook.token,
        handler=reconciler.project_change_number,
        workers=config.webhook.workers,
        queue_size=config.webhook.queue_size,
    )
    server.start()

    stop = threading.Event()

    def _shutdown(signum, _frame):
        logging.getLogger(__name__).info("signal %s, shutting down", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    sweeper = threading.Thread(
        target=_sweep_loop, args=(config, reconciler, stop), name="sweep", daemon=True
    )
    sweeper.start()

    try:
        while not stop.wait(1.0):
            pass
    finally:
        server.stop()
        sweeper.join(timeout=10)
    return 0


def _sweep_loop(config, reconciler, stop: threading.Event) -> None:
    log = logging.getLogger(__name__)
    while not stop.is_set():
        try:
            stats = reconciler.sweep()
            log.info(
                "sweep done: %d inspected, %d projected, %d unchanged, "
                "%d skipped, %d failed",
                stats.inspected,
                stats.projected,
                stats.unchanged,
                stats.skipped,
                stats.failed,
            )
        except Exception:  # noqa: BLE001 - the loop must outlive any single sweep
            log.exception("sweep failed")
        stop.wait(config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gerrit-github-archiver",
        description="Project Gerrit reviews onto GitHub pull requests.",
    )
    parser.add_argument("-c", "--config", required=True, help="path to config JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="inspect Gerrit and log intended actions without writing to GitHub",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sweep = sub.add_parser("sweep", help="run a single reconciliation pass")
    sweep.add_argument(
        "--full",
        action="store_true",
        help="ignore the lookback window and re-inspect every change "
        "(use for the cutover sweep)",
    )

    sub.add_parser("run", help="reconcile continuously on the configured interval")
    sub.add_parser(
        "serve",
        help="run the webhook receiver alongside the periodic sweep "
        "(the sweep still guarantees correctness)",
    )
    sub.add_parser("status", help="print ledger statistics")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = Config.load(args.config)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config = config.with_dry_run()

    ledger = Ledger(config.ledger_path)
    try:
        if args.command == "status":
            last = ledger.get_meta("last_sweep") or "never"
            print(f"ledger:     {config.ledger_path}")
            print(f"last sweep: {last}")
            print(f"projects:   {', '.join(p.gerrit_project for p in config.projects)}")
            return 0

        reconciler = Reconciler(config, ledger)
        if args.command == "run":
            reconciler.run_forever()
            return 0

        if args.command == "serve":
            return _serve(config, reconciler)

        stats = reconciler.sweep(full=args.full)
        print(
            f"inspected={stats.inspected} projected={stats.projected} "
            f"unchanged={stats.unchanged} skipped={stats.skipped} failed={stats.failed}"
        )
        for err in stats.errors[:20]:
            print(f"  error: {err}", file=sys.stderr)
        return 1 if stats.failed else 0
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
