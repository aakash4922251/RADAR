from __future__ import annotations

import argparse
import logging

from acquisition.config import settings
from acquisition.http import PublicHttpClient
from acquisition.registry import get_connector
from acquisition.scanner import scan_source
from db import database as db


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan a configured public procurement source once.")
    parser.add_argument("--source", default="cppp", help="Registered source identifier")
    args = parser.parse_args(argv)
    config = settings()
    if not config["enabled"]:
        raise SystemExit("Acquisition is disabled (ACQUISITION_ENABLED=false).")
    if args.source == "cppp" and not config["cppp_enabled"]:
        raise SystemExit("CPPP acquisition is disabled (CPPP_ENABLED=false).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init_db()
    client = PublicHttpClient(timeout=config["request_timeout"], max_retries=config["max_retries"], rate_limit_delay=config["rate_limit_delay"])
    connector_kwargs = {"client": client}
    if args.source == "cppp":
        connector_kwargs["search_url"] = config["cppp_search_url"]
    connector = get_connector(args.source, **connector_kwargs)
    with db.db_cursor() as cur:
        summary = scan_source(cur, connector, client=client, storage_root=config["storage_root"])
        print(summary)


if __name__ == "__main__":
    main()
