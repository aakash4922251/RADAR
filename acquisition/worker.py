from __future__ import annotations

import argparse
import logging
import time

from acquisition.config import settings
from acquisition.http import PublicHttpClient
from acquisition.registry import get_connector
from acquisition.scanner import scan_source
from db import database as db


def run_worker(source_id: str = "cppp", *, once: bool = False):
    config = settings()
    if not config["enabled"]:
        raise RuntimeError("Acquisition is disabled")
    db.init_db()
    client = PublicHttpClient(timeout=config["request_timeout"], max_retries=config["max_retries"], rate_limit_delay=config["rate_limit_delay"])
    connector_kwargs = {"client": client}
    if source_id == "cppp":
        connector_kwargs["search_url"] = config["cppp_search_url"]
    connector = get_connector(source_id, **connector_kwargs)
    interval_seconds = max(60, config["poll_interval_minutes"] * 60)
    while True:
        try:
            with db.db_cursor() as cur:
                summary = scan_source(cur, connector, client=client, storage_root=config["storage_root"])
                logging.getLogger(__name__).info("source=%s run_id=%s status=%s records=%s new=%s downloaded=%s processed=%s manual=%s failed=%s",
                    summary.source_id, summary.run_id, summary.status, summary.records_seen, summary.records_new,
                    summary.documents_downloaded, summary.documents_processed, summary.manual_action_required,
                    summary.documents_failed)
        except Exception:
            logging.getLogger(__name__).exception("source=%s scan failed; worker will continue", source_id)
        if once:
            return
        time.sleep(interval_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the public procurement acquisition worker.")
    parser.add_argument("--source", default="cppp")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_worker(args.source, once=args.once)


if __name__ == "__main__":
    main()
