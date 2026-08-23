#!/usr/bin/env python3
"""Launch Sub_Server.

Starts the data-node subscription in a background thread and serves the HTTPS
web interface on the local network.

    python3 run_server.py
    python3 run_server.py --port 8443 --node-host 10.0.0.5 --node-port 980
    python3 run_server.py --create-user alice

Configuration comes from ``config.toml`` (see ``config.example.toml``); the
command-line options below override it.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import signal
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from subserver import PROGRAM_NAME, __version__
from subserver.app import Services, create_app
from subserver.auth import AuthError, UserStore
from subserver.certs import ensure_certificate, local_addresses
from subserver.config import Config
from subserver.profiles import ProfileStore
from subserver.query import QueryEngine
from subserver.ringstore import RamStore
from subserver.subscriber import Subscriber
from subserver.userfuncs import FunctionRegistry

log = logging.getLogger("subserver")


def build_services(config: Config, *, connect: bool = True) -> Services:
    """Wire up the shared objects.  ``connect=False`` skips the data node."""
    store = RamStore(
        max_bytes=int(config.get("store", "max_bytes", 4_000_000_000)),
        max_age_seconds=float(config.get("store", "max_age_seconds", 0) or 0),
    )
    subscriber = None
    if connect:
        subscriber = Subscriber(
            store,
            host=str(config.get("data_node", "host", "127.0.0.1")),
            port=int(config.get("data_node", "port", 980)),
            variables=list(config.get("data_node", "variables", ["*"])),
            reconnect_min_delay=float(config.get("data_node", "reconnect_min_delay", 1.0)),
            reconnect_max_delay=float(config.get("data_node", "reconnect_max_delay", 30.0)),
            backfill_seconds=float(config.get("data_node", "backfill_seconds", 0) or 0),
            allow_history_requests=bool(config.get("data_node", "allow_history_requests", True)),
        )

    users = UserStore(config.data_dir / "users.json")
    profiles = ProfileStore(config.profiles_dir)
    functions = FunctionRegistry(
        config.functions_dir,
        enabled=bool(config.get("security", "allow_user_functions", True)),
    )
    queries = QueryEngine(
        store, subscriber, functions,
        max_points=int(config.get("plots", "max_points_per_series", 4000)),
    )
    return Services(config, store, subscriber, users, profiles, functions, queries)


def create_user_interactive(config: Config, username: str) -> int:
    users = UserStore(config.data_dir / "users.json")
    password = getpass.getpass(f"Password for {username!r}: ")
    if password != getpass.getpass("Confirm password: "):
        print("The passwords do not match.", file=sys.stderr)
        return 1
    try:
        record = users.create(username, password)
    except AuthError as exc:
        print(f"Could not create the user: {exc}", file=sys.stderr)
        return 1
    ProfileStore(config.profiles_dir).ensure_user_space(record["username"])
    print(f"Created user {record['username']!r}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", help="path to a TOML config file (default: ./config.toml)")
    parser.add_argument("--host", help="address to bind the web server to")
    parser.add_argument("--port", type=int, help="HTTPS port")
    parser.add_argument("--node-host", help="data access node host")
    parser.add_argument("--node-port", type=int, help="data access node port")
    parser.add_argument("--no-subscriber", action="store_true",
                        help="serve the web interface without connecting to a data node")
    parser.add_argument("--create-user", metavar="USERNAME",
                        help="create a user account and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")
    parser.add_argument("--version", action="version", version=f"{PROGRAM_NAME} {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
    )

    config = Config.load(args.config)
    for section, key, value in (
        ("server", "host", args.host),
        ("server", "port", args.port),
        ("data_node", "host", args.node_host),
        ("data_node", "port", args.node_port),
    ):
        if value is not None:
            config.section(section)[key] = value

    if args.create_user:
        return create_user_interactive(config, args.create_user)

    config.data_dir.mkdir(parents=True, exist_ok=True)
    services = build_services(config, connect=not args.no_subscriber)
    app = create_app(services)

    try:
        cert_file, key_file = ensure_certificate(
            config.path("server", "cert_file"),
            config.path("server", "key_file"),
            list(config.get("server", "cert_hostnames", [])),
        )
    except RuntimeError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 2

    host = str(config.get("server", "host", "0.0.0.0"))
    port = int(config.get("server", "port", 8443))

    if services.subscriber:
        services.subscriber.start()

    from werkzeug.serving import make_server

    try:
        server = make_server(host, port, app, threaded=True, ssl_context=(str(cert_file), str(key_file)))
    except OSError as exc:
        print(f"\nCould not bind to {host}:{port} -- {exc}", file=sys.stderr)
        if getattr(exc, "errno", None) == 13 and port < 1024:
            print("Ports below 1024 need root. Pick a higher port with --port.", file=sys.stderr)
        return 2

    _print_banner(host, port, services, users_exist=services.users.count() > 0)

    stopping = threading.Event()

    def shutdown(_signum: int, _frame: object) -> None:
        if not stopping.is_set():
            stopping.set()
            log.info("shutting down...")
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    finally:
        if services.subscriber:
            services.subscriber.stop()
        log.info("stopped")
    return 0


def _print_banner(host: str, port: int, services: Services, users_exist: bool) -> None:
    addresses = ["127.0.0.1"] if host in ("127.0.0.1", "localhost") else local_addresses()
    node = services.subscriber
    print(f"\n  {PROGRAM_NAME} {__version__}")
    print(f"  {'-' * (len(PROGRAM_NAME) + len(__version__) + 1)}")
    print("  Web interface:")
    for address in addresses:
        print(f"    https://{address}:{port}/")
    if host not in ("127.0.0.1", "localhost"):
        print(f"    https://{socket.gethostname()}:{port}/")
    print("\n  The certificate is self-signed, so browsers will warn on the first visit.")
    print("  Trust data/certs/server.crt to silence that.")
    if node:
        print(f"\n  Data node: {node.host}:{node.port} (connecting in the background)")
    else:
        print("\n  Data node: disabled (--no-subscriber)")
    if not users_exist:
        print("\n  No accounts exist yet. Use 'Create a new user' on the sign-in page,")
        print("  or run:  python3 run_server.py --create-user <name>")
    print("\n  Press Ctrl+C to stop.\n")


if __name__ == "__main__":
    raise SystemExit(main())
