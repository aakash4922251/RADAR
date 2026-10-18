"""Historical procurement dataset import/query support.

This module treats large historical datasets as an analytical side-store (DuckDB +
Parquet), not as rows to be copied into the application's transactional SQLite DB.
It supports the public cpp-tender-audit GitHub release and similar GitHub repos that
publish Parquet release assets.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_REPO = "https://github.com/dhrubasumatary/cpp-tender-audit"
GITHUB_API = "https://api.github.com"


@dataclass
class DatasetAsset:
    name: str
    url: str
    size_bytes: int
    sha256: str | None
    table: str | None
    year: str | None

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


def parse_github_repo(url: str) -> tuple[str, str] | None:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        return None
    return parts[0], parts[1].removesuffix(".git")


def _github_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Government-Procurement-Opportunity-Radar/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def inspect_github_dataset(repo_url: str = DEFAULT_REPO) -> dict[str, Any]:
    repo = parse_github_repo(repo_url)
    if not repo:
        raise ValueError("Enter a public GitHub repository URL such as https://github.com/owner/repository")
    owner, name = repo
    repo_api = f"{GITHUB_API}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
    metadata = _github_json(repo_api)
    releases = _github_json(repo_api + "/releases?per_page=10")
    if not releases:
        raise ValueError("This GitHub repository has no public releases to import.")
    release = releases[0]
    assets: list[DatasetAsset] = []
    for asset in release.get("assets", []):
        asset_name = asset.get("name", "")
        if not asset_name.lower().endswith(".parquet"):
            continue
        m = re.search(r"^(?P<table>.+?)__year__(?P<year>[^.]+)\.parquet$", asset_name)
        table = m.group("table") if m else None
        year = m.group("year") if m else None
        # GitHub's release API does not expose SHA-256 for assets; manifest.json does.
        assets.append(DatasetAsset(asset_name, asset["browser_download_url"], int(asset.get("size", 0)), None, table, year))

    manifest = None
    try:
        manifest_url = f"https://raw.githubusercontent.com/{owner}/{name}/{release['tag_name']}/manifest.json"
        manifest = _github_json(manifest_url)
    except Exception:
        manifest = None
    if isinstance(manifest, dict):
        by_name = {str(item.get("path", "")).split("/")[-1]: item for item in manifest.get("files", []) if isinstance(item, dict)}
        # Some manifests use a top-level list.
        if not by_name and isinstance(manifest.get("partitions"), list):
            by_name = {str(item.get("path", "")).split("/")[-1]: item for item in manifest["partitions"] if isinstance(item, dict)}
        for a in assets:
            item = by_name.get(a.name)
            if item:
                a.sha256 = item.get("sha256") or item.get("sha")

    return {
        "repo": f"{owner}/{name}",
        "repo_url": repo_url,
        "release": release.get("tag_name"),
        "release_name": release.get("name"),
        "published_at": release.get("published_at"),
        "description": metadata.get("description"),
        "assets": assets,
    }


def download_asset(asset: DatasetAsset, destination: str | Path, progress: Callable[[int, int], None] | None = None) -> Path:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    req = urllib.request.Request(asset.url, headers={"User-Agent": "Government-Procurement-Opportunity-Radar/1.0"})
    digest = hashlib.sha256()
    downloaded = 0
    with urllib.request.urlopen(req, timeout=900) as response, open(tmp, "wb") as out:
        total = int(response.headers.get("Content-Length") or asset.size_bytes or 0)
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            downloaded += len(chunk)
            if progress:
                progress(downloaded, total)
    actual = digest.hexdigest()
    if asset.sha256 and actual.lower() != asset.sha256.lower():
        tmp.unlink(missing_ok=True)
        raise ValueError(f"SHA-256 mismatch for {asset.name}: expected {asset.sha256}, got {actual}")
    tmp.replace(destination)
    return destination


def register_imported_dataset(root: str | Path, metadata: dict[str, Any], assets: list[DatasetAsset]) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "source.json"
    payload = {
        "source": metadata,
        "assets": [a.__dict__ for a in assets],
    }
    manifest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return manifest


def duckdb_available():
    try:
        import duckdb  # type: ignore
        return duckdb
    except ImportError:
        return None


def _aoc_date_sql(column: str = "aoc_date") -> str:
    """Parse the CPPP AOC date formats used by the public dataset.

    The source stores dates as strings such as ``17-Jul-2024 05:30 PM``.
    A plain CAST/TRY_CAST is not reliable for that format, so all historical
    AOC features use explicit parsing first and ISO parsing as a fallback.
    """
    c = f"CAST({column} AS VARCHAR)"
    return (
        f"COALESCE("
        f"TRY_CAST(TRY_STRPTIME({c}, '%d-%b-%Y %I:%M %p') AS DATE), "
        f"TRY_CAST(TRY_STRPTIME({c}, '%d-%b-%Y') AS DATE), "
        f"TRY_CAST(TRY_STRPTIME({c}, '%Y-%m-%d %H:%M:%S') AS DATE), "
        f"TRY_CAST({c} AS DATE)"
        f")"
    )


def query_imported_dataset(root: str | Path, sql: str):
    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    root = Path(root)
    con = duckdb.connect()
    # Expose imported partitions as simple views. The user can query only local files.
    for table in {"aoc_tenders", "aoc_details", "tenders", "tender_details"}:
        pattern = str(root / table / "year=*" / "data.parquet").replace("\\", "/")
        files = list((root / table).glob("year=*/data.parquet")) if (root / table).exists() else []
        if files:
            # DuckDB cannot prepare parameters for CREATE VIEW statements.
            # The path is generated locally by the application, so quote the
            # SQL string literal explicitly instead of binding it as a parameter.
            safe_pattern = pattern.replace("'", "''")
            con.execute(f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet('{safe_pattern}')")
    return con.execute(sql).fetchdf()


def find_historical_aoc_root(base_dir: str | Path) -> Path | None:
    """Return the first imported historical dataset containing AOC partitions."""
    base = Path(base_dir)
    if not base.exists():
        return None
    candidates = sorted(base.glob("*/aoc_tenders/year=*/data.parquet"))
    if not candidates:
        return None
    return candidates[0].parents[2]


def query_historical_aocs(root: str | Path, search: str = "", year: str = "All years", portal: str = "All portals", limit: int | None = 50):
    """Query imported AOC listing partitions through DuckDB."""
    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    root = Path(root)
    con = duckdb.connect()
    pattern = str(root / "aoc_tenders" / "year=*" / "data.parquet").replace("\\", "/")
    safe_pattern = pattern.replace("'", "''")
    con.execute(f"CREATE OR REPLACE VIEW aoc_tenders AS SELECT * FROM read_parquet('{safe_pattern}')")
    where, params = [], []
    if year and year != "All years":
        where.append("CAST(year AS VARCHAR) = ?")
        params.append(str(year))
    if portal and portal != "All portals":
        where.append("LOWER(COALESCE(portal_type, '')) = LOWER(?)")
        params.append(portal)
    if search.strip():
        term = f"%{search.strip()}%"
        where.append("(LOWER(COALESCE(title, '')) LIKE LOWER(?) OR LOWER(COALESCE(org_name, '')) LIKE LOWER(?) OR LOWER(COALESCE(tender_id, '')) LIKE LOWER(?) OR LOWER(COALESCE(ref_no, '')) LIKE LOWER(?))")
        params.extend([term] * 4)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""SELECT year, aoc_date, org_name, title, tender_id, ref_no, portal_type, detail_url
              FROM aoc_tenders{clause}
              ORDER BY {_aoc_date_sql("aoc_date")} DESC NULLS LAST, year DESC
              {f'LIMIT {int(limit)}' if limit else ''}"""
    return con.execute(sql, params).fetchdf()


def historical_aoc_stats(root: str | Path) -> dict[str, Any]:
    duckdb = duckdb_available()
    if duckdb is None:
        return {}
    root = Path(root)
    con = duckdb.connect()
    pattern = str(root / "aoc_tenders" / "year=*" / "data.parquet").replace("\\", "/")
    safe_pattern = pattern.replace("'", "''")
    row = con.execute(f"""
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT CAST(year AS VARCHAR)) AS years,
               COUNT(DISTINCT org_name) AS organisations,
               LIST(DISTINCT CAST(year AS VARCHAR) ORDER BY CAST(year AS VARCHAR)) AS year_values
        FROM read_parquet('{safe_pattern}', union_by_name = true)
        WHERE year IS NOT NULL
    """).fetchone()
    partition_years = sorted({p.parent.name.split('=', 1)[1] for p in (root / 'aoc_tenders').glob('year=*/data.parquet') if '=' in p.parent.name}) if (root / 'aoc_tenders').exists() else []
    year_values = sorted([str(x) for x in (row[3] or [])])
    return {
        "total": int(row[0]),
        "years": int(row[1]),
        "organisations": int(row[2]),
        "year_values": year_values,
        "partition_years": partition_years,
    }


def _as_date(value):
    import datetime as _dt
    if value is None:
        return _dt.date.today()
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, str):
        return _dt.date.fromisoformat(value)
    return value


def _aoc_filters(state: str = "", city: str = "", keyword: str = "") -> tuple[str, list]:
    """SQL AND-clauses for the user-facing filters. User-typed values are bound
    as parameters, never interpolated into SQL."""
    filters, params = [], []
    state, city, keyword = (state or "").strip(), (city or "").strip(), (keyword or "").strip()
    if state and state.lower() != "all":
        filters.append("(LOWER(COALESCE(state, '')) LIKE LOWER(?) OR LOWER(org_name) LIKE LOWER(?))")
        params.extend([f"%{state}%"] * 2)
    if city and city.lower() != "all":
        filters.append("(LOWER(org_name) LIKE LOWER(?) OR LOWER(title) LIKE LOWER(?))")
        params.extend([f"%{city}%"] * 2)
    for word in keyword.split():
        filters.append("LOWER(title) LIKE LOWER(?)")
        params.append(f"%{word}%")
    return "".join(f"\n          AND {f}" for f in filters), params


_FAMILY_STOPWORDS = (
    "annual|comprehensive|maintenance|contract|contracts|services?|supply|supplies|providing|provision|"
    "procurement|work|works|for|of|the|at|and|in|to|on|by|from|with|under|through|hiring|engagement|rate|"
    "tender|tenders|etender|nit|bid|call|selection|appointment|amc|camc|repair|repairs|operation|operations|"
    "year|years|yearly|financial|period|please|refer|notice|various|other|etc|[a-z]{1,2}"
)


def _org_key_sql(column: str) -> str:
    """Buyer identity: lower-cased, alphanumerics only. "||" separators and
    missing spaces around them (both occur in CPPP data) normalise alike."""
    return (f"trim(regexp_replace(regexp_replace(LOWER(COALESCE({column}, '')), '\\|\\|', '', 'g'), "
            f"'[^a-z0-9]+', ' ', 'g'))")


def _family_key_sql(column: str) -> str:
    """Requirement family: the title minus the per-cycle reference after "/ ",
    digits, HTML entities and generic procurement words."""
    return f"""trim(regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(LOWER(COALESCE({column}, '')), '(?s)/\\s.*$', ''),
                            '&#?[a-z0-9]+;', ' ', 'g'
                        ),
                        '[^a-z]+', ' ', 'g'
                    ),
                    '\\b({_FAMILY_STOPWORDS})\\b', ' ', 'g'
                ),
                '\\s+', ' ', 'g'
            ))"""


def _aoc_awards_ctes(safe_pattern: str, filter_sql: str, date_column: str = "aoc_date") -> str:
    """CTEs ``raw``, ``base`` and ``dedup``: one row per distinct dated award,
    keyed by buyer (``org_key``) and requirement family (``title_family_key``).

    Identity: same buyer + a normalized requirement family. CPPP titles carry
    the tender reference after "/ " (e.g. "Housekeeping at X/ NIT/E1571/20-21"),
    which changes every cycle, so it is cut off along with digits, HTML
    entities and generic procurement words; what remains is the stable
    description of the need. Titles with fewer than two meaningful words
    ("REPAIR", "please refer tender notice") are too vague to identify a
    requirement and are excluded rather than merged.

    ``date_column`` picks the event date: ``aoc_date`` (award published) or
    ``closing_date`` (bid submission closed, i.e. when the tender ran).
    """
    if date_column not in ("aoc_date", "closing_date"):
        raise ValueError(f"unsupported date column: {date_column}")
    return f"""
    raw AS (
        SELECT
            {_aoc_date_sql(date_column)} AS award_date,
            NULLIF(TRIM(org_name), '') AS org_name,
            NULLIF(TRIM(title), '') AS title,
            tender_id, ref_no, portal_type, detail_url, state,
            {_org_key_sql('org_name')} AS org_key,
            regexp_replace(
                regexp_replace(LOWER(TRIM(COALESCE(title, ''))), '[^a-z0-9]+', ' ', 'g'),
                '\\s+', ' ', 'g'
            ) AS title_key,
            {_family_key_sql('title')} AS title_family_key
        FROM read_parquet('{safe_pattern}', union_by_name = true)
        WHERE {_aoc_date_sql(date_column)} IS NOT NULL
          AND COALESCE(TRIM(org_name), '') <> ''
          AND COALESCE(TRIM(title), '') <> ''{filter_sql}
    ),
    base AS (
        SELECT * FROM raw
        WHERE len(string_split(title_family_key, ' ')) >= 2
    ),
    dedup AS (
        SELECT * EXCLUDE (rn)
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY org_key, title_key, award_date, COALESCE(tender_id, ''), COALESCE(ref_no, '')
                ORDER BY award_date
            ) AS rn
            FROM base
        )
        WHERE rn = 1
    )"""


def historical_aoc_cycle_signals(root: str | Path, as_of=None, horizon_days: int = 365, limit: int | None = 100, state: str = "", city: str = "", keyword: str = "", date_column: str = "aoc_date", overdue_days: int = 45):
    """Return evidence-backed recurring AOC opportunity windows.

    Uses exact buyer + normalized requirement title as the conservative identity.
    Two dated awards are enough to create an EARLY signal (one observed interval);
    three or more awards create MEDIUM/HIGH signals with a more stable median cycle.
    Only awards dated before ``as_of`` are used, so a past ``as_of`` reproduces
    exactly what would have been predicted then (see backtest_aoc_signals).
    Requirements whose expected next award is more than ``overdue_days``
    before ``as_of`` are left out. This is a re-opportunity signal, never a
    confirmed future tender.
    """
    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    root = Path(root)
    pattern = str(root / "aoc_tenders" / "year=*" / "data.parquet").replace("\\", "/")
    safe_pattern = pattern.replace("'", "''")
    import datetime as _dt
    as_of = _as_date(as_of)
    horizon = as_of + _dt.timedelta(days=max(1, int(horizon_days)))
    filter_sql, params = _aoc_filters(state, city, keyword)

    con = duckdb.connect()
    sql = f"""
    WITH {_aoc_awards_ctes(safe_pattern, filter_sql, date_column)},
    dated AS (
        SELECT *,
               LAG(award_date) OVER (PARTITION BY org_key, title_family_key ORDER BY award_date) AS prev_award_date
        FROM dedup
        WHERE award_date < DATE '{as_of.isoformat()}'
    ),
    gaps AS (
        SELECT *, DATE_DIFF('day', prev_award_date, award_date) AS gap_days
        FROM dated
        WHERE prev_award_date IS NOT NULL
          AND DATE_DIFF('day', prev_award_date, award_date) BETWEEN 30 AND 2190
    ),
    stats AS (
        SELECT
            org_key, title_family_key AS title_key,
            COUNT(*) + 1 AS award_count,
            MIN(prev_award_date) AS first_award_date,
            MAX(award_date) AS last_award_date,
            CAST(ROUND(quantile_cont(gap_days, 0.50)) AS INTEGER) AS median_gap_days,
            CAST(ROUND(quantile_cont(gap_days, 0.25)) AS INTEGER) AS q1_gap_days,
            CAST(ROUND(quantile_cont(gap_days, 0.75)) AS INTEGER) AS q3_gap_days,
            MAX(org_name) AS org_name,
            trim(regexp_replace(MAX_BY(title, award_date), '(&#?[a-zA-Z0-9]+;|\\s)+', ' ', 'g')) AS title,
            MAX_BY(state, award_date) AS state,
            MAX_BY(tender_id, award_date) AS last_tender_id,
            MAX_BY(ref_no, award_date) AS last_ref_no,
            MAX_BY(portal_type, award_date) AS portal_type,
            MAX_BY(detail_url, award_date) AS last_detail_url
        FROM gaps
        GROUP BY org_key, title_family_key
        HAVING COUNT(*) >= 1
    ),
    signals AS (
        SELECT *,
               last_award_date + median_gap_days AS expected_next_date,
               GREATEST(
                   30,
                   CAST(ROUND(GREATEST(0, q3_gap_days - q1_gap_days) * 0.50) AS INTEGER),
                   CAST(ROUND(median_gap_days * CASE WHEN award_count >= 3 THEN 0.20 ELSE 0.30 END) AS INTEGER)
               ) AS window_half_width
        FROM stats
        WHERE median_gap_days BETWEEN 90 AND 1095
    )
    SELECT
        org_key, title_key AS family_key,
        org_name, title, state, award_count, first_award_date, last_award_date,
        median_gap_days, q1_gap_days, q3_gap_days, expected_next_date,
        expected_next_date - window_half_width AS window_start,
        expected_next_date + window_half_width AS window_end,
        last_tender_id, last_ref_no, portal_type, last_detail_url,
        CASE
          WHEN award_count >= 6 AND (q3_gap_days - q1_gap_days) <= median_gap_days THEN 'HIGH'
          WHEN award_count >= 4 THEN 'MEDIUM'
          WHEN award_count >= 3 THEN 'EARLY'
          ELSE 'EARLY'
        END AS signal_strength
    FROM signals
    WHERE expected_next_date BETWEEN DATE '{as_of.isoformat()}' - {int(overdue_days)} AND DATE '{horizon.isoformat()}'
    ORDER BY
        CASE WHEN DATE '{as_of.isoformat()}' BETWEEN window_start AND window_end THEN 0 ELSE 1 END,
        ABS(DATE_DIFF('day', DATE '{as_of.isoformat()}', expected_next_date)),
        award_count DESC
    {f'LIMIT {int(limit)}' if limit else ''}
    """
    return con.execute(sql, params).fetchdf()



def backtest_aoc_signals(root: str | Path, as_of, horizon_days: int = 182, state: str = "", city: str = "", keyword: str = "", date_column: str = "aoc_date") -> dict[str, Any]:
    """Score the re-tender predictions that would have been made on ``as_of``.

    Predictions are generated from awards dated before ``as_of`` only, then
    checked against the awards that actually followed. A prediction is a HIT
    when the same buyer awarded the same requirement family again inside the
    predicted window. ``baseline_hit_rate`` scores an equally wide window half
    a cycle off, so the gap between the two shows how much the timing itself
    is worth. Only predictions whose windows have fully closed by the end of
    the available data are scored; the rest are reported as pending.
    """
    import datetime as _dt
    import pandas as pd

    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    as_of = _as_date(as_of)
    root = Path(root)
    pattern = str(root / "aoc_tenders" / "year=*" / "data.parquet").replace("\\", "/")
    safe_pattern = pattern.replace("'", "''")
    filter_sql, params = _aoc_filters(state, city, keyword)

    signals = historical_aoc_cycle_signals(root, as_of=as_of, horizon_days=horizon_days, limit=None,
                                           state=state, city=city, keyword=keyword, date_column=date_column)
    con = duckdb.connect()
    # The source has a thin tail of typo'd dates after its real cut-off (a few
    # hundred rows a month, some in year 9999), so "end of data" is the 99.9th
    # percentile award date, not the maximum. Windows closing after it can't be
    # checked yet and stay pending instead of counting as misses.
    future = con.execute(f"""
        WITH {_aoc_awards_ctes(safe_pattern, filter_sql, date_column)},
        cutoff AS (SELECT quantile_disc(award_date, 0.999) AS data_end FROM dedup)
        SELECT org_key, title_family_key AS family_key, award_date, cutoff.data_end
        FROM dedup, cutoff
        WHERE award_date >= DATE '{as_of.isoformat()}' AND award_date <= cutoff.data_end
    """, params).fetchdf()
    data_end = future["data_end"].iloc[0].date() if not future.empty else as_of
    future = future.drop(columns=["data_end"])

    detail = signals[["org_key", "family_key", "org_name", "title", "signal_strength", "award_count",
                      "median_gap_days", "last_award_date", "expected_next_date", "window_start", "window_end"]].copy()
    detail["window_days"] = (detail["window_end"] - detail["window_start"]).dt.days
    # Chance baseline: the same-width window moved half a cycle later, i.e. at
    # the time the cycle says is least likely. If timing carries information,
    # the real window must hit clearly more often than this one.
    shift = pd.to_timedelta(detail["median_gap_days"] // 2, unit="D")
    detail["baseline_start"] = detail["window_start"] + shift
    detail["baseline_end"] = detail["window_end"] + shift
    detail["scored"] = detail["baseline_end"].dt.date <= data_end

    merged = detail[["org_key", "family_key", "window_start", "window_end", "baseline_start", "baseline_end"]].merge(
        future, on=["org_key", "family_key"], how="inner")

    def _keys_with_award_between(start_col, end_col):
        inside = merged[(merged["award_date"] >= merged[start_col]) & (merged["award_date"] <= merged[end_col])]
        return set(zip(inside["org_key"], inside["family_key"]))

    hit_keys = _keys_with_award_between("window_start", "window_end")
    baseline_keys = _keys_with_award_between("baseline_start", "baseline_end")
    first_after = merged.groupby(["org_key", "family_key"])["award_date"].min()

    keys = list(zip(detail["org_key"], detail["family_key"]))
    detail["hit"] = [k in hit_keys for k in keys]
    detail["baseline_hit"] = [k in baseline_keys for k in keys]
    detail["next_actual_award"] = [first_after.get(k) for k in keys]
    detail["error_days"] = [
        (a - e).days if a is not None and not pd.isna(a) else None
        for a, e in zip(detail["next_actual_award"], detail["expected_next_date"])
    ]

    rows = []
    for label, group in [("All", detail)] + [(s, detail[detail["signal_strength"] == s]) for s in ("HIGH", "MEDIUM", "EARLY")]:
        scored = group[group["scored"]]
        errors = scored["error_days"].dropna().abs()
        rows.append({
            "evidence": label,
            "predicted": len(group),
            "scored": len(scored),
            "pending": len(group) - len(scored),
            "hits": int(scored["hit"].sum()),
            "hit_rate": round(scored["hit"].mean(), 3) if len(scored) else None,
            "baseline_hit_rate": round(scored["baseline_hit"].mean(), 3) if len(scored) else None,
            "retendered_by_data_end": round(scored["next_actual_award"].notna().mean(), 3) if len(scored) else None,
            "median_abs_error_days": int(errors.median()) if len(errors) else None,
            "median_window_days": int(scored["window_days"].median()) if len(scored) else None,
        })
    return {"as_of": as_of, "horizon_days": horizon_days, "data_end": data_end,
            "summary": pd.DataFrame(rows), "detail": detail.drop(columns=["org_key", "family_key", "baseline_start", "baseline_end"])}
