from __future__ import annotations

import argparse
import json

from acquisition.config import settings
from acquisition.http import PublicHttpClient
from acquisition.registry import get_connector


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check public procurement source health.")
    parser.add_argument("--source", default="cppp")
    args = parser.parse_args(argv)
    config = settings()
    client = PublicHttpClient(timeout=config["request_timeout"], max_retries=0, rate_limit_delay=config["rate_limit_delay"])
    connector_kwargs = {"client": client}
    if args.source == "cppp":
        connector_kwargs["search_url"] = config["cppp_search_url"]
    result = get_connector(args.source, **connector_kwargs).health_check()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
