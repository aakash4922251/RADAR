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


def historical_aoc_cycle_signals(root: str | Path, as_of=None, horizon_days: int = 365, limit: int | None = 100, state: str = "", city: str = ""):
    """Return evidence-backed recurring AOC opportunity windows.

    Uses exact buyer + normalized requirement title as the conservative identity.
    Two dated awards are enough to create an EARLY signal (one observed interval);
    three or more awards create MEDIUM/HIGH signals with a more stable median cycle.
    This is a re-opportunity signal, never a confirmed future tender.
    """
    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    root = Path(root)
    pattern = str(root / "aoc_tenders" / "year=*" / "data.parquet").replace("\\", "/")
    safe_pattern = pattern.replace("'", "''")
    import datetime as _dt
    if as_of is None:
        as_of = _dt.date.today()
    elif isinstance(as_of, _dt.datetime):
        as_of = as_of.date()
    elif isinstance(as_of, str):
        as_of = _dt.date.fromisoformat(as_of)
    horizon = as_of + _dt.timedelta(days=max(1, int(horizon_days)))

    con = duckdb.connect()
    # Identity: same buyer + a normalized requirement family. Generic procurement
    # words are removed so common wording changes (AMC/maintenance/services/etc.)
    # can still form a family, while the buyer constraint prevents cross-organisation
    # merging. Two awards yield an EARLY signal; 3+ yield stronger evidence.
    sql = f"""
    WITH base AS (
        SELECT
            {_aoc_date_sql("aoc_date")} AS award_date,
            NULLIF(TRIM(org_name), '') AS org_name,
            NULLIF(TRIM(title), '') AS title,
            tender_id, ref_no, portal_type, detail_url,
            regexp_replace(
                regexp_replace(LOWER(TRIM(COALESCE(org_name, ''))), '[^a-z0-9]+', ' ', 'g'),
                '\\s+', ' ', 'g'
            ) AS org_key,
            regexp_replace(
                regexp_replace(LOWER(TRIM(COALESCE(title, ''))), '[^a-z0-9]+', ' ', 'g'),
                '\\s+', ' ', 'g'
            ) AS title_key,
            trim(regexp_replace(
                regexp_replace(
                    regexp_replace(LOWER(TRIM(COALESCE(title, ''))), '[^a-z0-9]+', ' ', 'g'),
                    '\\b(annual|comprehensive|maintenance|contract|services?|service|supply|supplies|providing|procurement|work|works|for|of|the|at|and|in|to|on|through|hiring|engagement|rate|tender|bid|selection|appointment|amc|camc|repair|repairs|operation|operations)\\b', ' ', 'g'
                ),
                '\\s+', ' ', 'g'
            )) AS title_family_key
        FROM read_parquet('{safe_pattern}', union_by_name = true)
        WHERE {_aoc_date_sql("aoc_date")} IS NOT NULL
          AND COALESCE(TRIM(org_name), '') <> ''
          AND COALESCE(TRIM(title), '') <> ''
          {f" AND LOWER(org_name) LIKE '%{state.lower()}%'" if state and state.lower() != 'all' else ""}
          {f" AND (LOWER(org_name) LIKE '%{city.lower()}%' OR LOWER(title) LIKE '%{city.lower()}%')" if city and city.lower() != 'all' else ""}
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
    ),
    dated AS (
        SELECT *,
               LAG(award_date) OVER (PARTITION BY org_key, COALESCE(NULLIF(title_family_key, ''), title_key) ORDER BY award_date) AS prev_award_date
        FROM dedup
    ),
    gaps AS (
        SELECT *, DATE_DIFF('day', prev_award_date, award_date) AS gap_days
        FROM dated
        WHERE prev_award_date IS NOT NULL
          AND DATE_DIFF('day', prev_award_date, award_date) BETWEEN 30 AND 2190
    ),
    stats AS (
        SELECT
            org_key, COALESCE(NULLIF(title_family_key, ''), title_key) AS title_key,
            COUNT(*) + 1 AS award_count,
            MIN(prev_award_date) AS first_award_date,
            MAX(award_date) AS last_award_date,
            CAST(ROUND(quantile_cont(gap_days, 0.50)) AS INTEGER) AS median_gap_days,
            CAST(ROUND(quantile_cont(gap_days, 0.25)) AS INTEGER) AS q1_gap_days,
            CAST(ROUND(quantile_cont(gap_days, 0.75)) AS INTEGER) AS q3_gap_days,
            MAX(org_name) AS org_name,
            MAX(title) AS title,
            MAX_BY(tender_id, award_date) AS last_tender_id,
            MAX_BY(ref_no, award_date) AS last_ref_no,
            MAX_BY(portal_type, award_date) AS portal_type,
            MAX_BY(detail_url, award_date) AS last_detail_url
        FROM gaps
        GROUP BY org_key, COALESCE(NULLIF(title_family_key, ''), title_key)
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
        org_name, title, award_count, first_award_date, last_award_date,
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
    WHERE expected_next_date BETWEEN DATE '{as_of.isoformat()}' - 45 AND DATE '{horizon.isoformat()}'
    ORDER BY
        CASE WHEN DATE '{as_of.isoformat()}' BETWEEN window_start AND window_end THEN 0 ELSE 1 END,
        ABS(DATE_DIFF('day', DATE '{as_of.isoformat()}', expected_next_date)),
        award_count DESC
    {f'LIMIT {int(limit)}' if limit else ''}
    """
    return con.execute(sql).fetchdf()

