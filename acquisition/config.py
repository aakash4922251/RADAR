from __future__ import annotations

import os


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def settings() -> dict:
    return {
        "enabled": env_bool("ACQUISITION_ENABLED", True),
        "cppp_enabled": env_bool("CPPP_ENABLED", True),
        "poll_interval_minutes": int(os.environ.get("CPPP_POLL_INTERVAL_MINUTES", "15")),
        "request_timeout": float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30")),
        "max_retries": int(os.environ.get("MAX_RETRIES", "3")),
        "rate_limit_delay": float(os.environ.get("RATE_LIMIT_DELAY_SECONDS", "1")),
        "storage_root": os.environ.get("ACQUISITION_STORAGE_ROOT", "data/acquired"),
        "cppp_search_url": os.environ.get(
            "CPPP_SEARCH_URL", "https://eprocure.gov.in/eprocure/app"
        ),
    }
