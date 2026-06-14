#!/usr/bin/env python3
"""
Create the GitHub repository metadata cache used by the dataset setup step.

Input:
  - Devpost projects CSV containing a column with GitHub repository links
  - Default input path: dataset/dataset/Projects Dataset/all_project_details.csv
  - Default GitHub link column: "GitHub link"

Output:
  - SQLite database, by default github_repo_cache.sqlite
  - Main table: repos_cache

Authentication:
  - This script reads a GitHub API token from the GITHUB_TOKEN environment variable.
  - It can run without a token, but unauthenticated GitHub API requests are strongly
    rate limited and may fail before the full dataset is cached.

Pipeline order:
  1. collect_github_repo_cache.py
     creates github_repo_cache.sqlite / repos_cache
  2. setup_dataset_github_enrichment.py
     reads github_repo_cache.sqlite and creates dataset_github_enriched.db
  3. filter_dataset.py
     reads dataset_github_enriched.db and creates data_filtered.db
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests


GITHUB_REPO_RE = re.compile(
    r"""^
        (?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})
        /
        (?P<repo>[A-Za-z0-9._-]+)
        $
    """,
    re.X,
)


def extract_urls_maybe_list(value: Any) -> list[str]:
    """Extract candidate URLs from plain strings, list-like strings, or Python lists."""
    if pd.isna(value):
        return []

    if isinstance(value, list):
        return [str(url).strip() for url in value if str(url).strip()]

    text = str(value).strip()
    if not text:
        return []

    if (text.startswith("[") and text.endswith("]")) or (text.startswith("(") and text.endswith(")")):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(url).strip() for url in parsed if str(url).strip()]
        except Exception:
            pass

    return [part for part in re.split(r"[,\s|]+", text) if part]


def owner_repo_from_github_url(url: str) -> tuple[str | None, str | None]:
    """
    Extract (owner, repo) from a GitHub repository URL.

    Handles:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - extra paths such as /tree/main or /issues/1 by keeping only owner/repo
    """
    try:
        parsed = urlparse(url)
        if "github.com" not in (parsed.netloc or "").lower():
            return None, None

        parts = [part for part in (parsed.path or "").split("/") if part]
        if len(parts) < 2:
            return None, None

        owner, repo = parts[0], parts[1]
        repo = repo[:-4] if repo.endswith(".git") else repo

        if not GITHUB_REPO_RE.match(f"{owner}/{repo}"):
            return None, None

        return owner, repo
    except Exception:
        return None, None


def extract_unique_repositories(projects: pd.DataFrame, github_col: str) -> pd.DataFrame:
    """Extract one unique owner/repo pair for every valid GitHub repository URL found."""
    records: list[dict[str, str]] = []

    for value in projects[github_col]:
        for url in extract_urls_maybe_list(value):
            owner, repo = owner_repo_from_github_url(url)
            if owner and repo:
                records.append({
                    "owner": owner,
                    "repo": repo,
                    "repo_key": f"{owner.lower()}/{repo.lower()}",
                    "source_url": url,
                })

    repos = pd.DataFrame(records)
    if repos.empty:
        return pd.DataFrame(columns=["owner", "repo", "repo_key", "source_url"])

    repos = repos.drop_duplicates("repo_key").sort_values(["owner", "repo"]).reset_index(drop=True)
    return repos


def ensure_schema(conn: sqlite3.Connection, table: str) -> None:
    """Create the cache table if it does not already exist."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            owner TEXT NOT NULL,
            repo TEXT NOT NULL,
            repo_key TEXT NOT NULL PRIMARY KEY,
            source_url TEXT,

            full_name TEXT,
            html_url TEXT,
            description TEXT,
            homepage TEXT,

            private INTEGER,
            fork INTEGER,
            archived INTEGER,
            disabled INTEGER,
            is_template INTEGER,

            language TEXT,
            default_branch TEXT,
            size_kb INTEGER,

            stars INTEGER,
            forks INTEGER,
            watchers INTEGER,
            open_issues INTEGER,

            created_at TEXT,
            updated_at TEXT,
            pushed_at TEXT,

            raw_json TEXT,

            status_code INTEGER,
            error TEXT,
            fetched_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def existing_repo_keys(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"SELECT repo_key FROM {table}").fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()


def bool_to_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(bool(value))


def repo_payload_to_record(
    owner: str,
    repo: str,
    repo_key: str,
    source_url: str | None,
    status_code: int,
    payload: dict[str, Any] | None,
    error: str | None,
) -> dict[str, Any]:
    """Convert a GitHub API response into the cache table schema."""
    now = datetime.now(timezone.utc).isoformat()

    if not payload:
        return {
            "owner": owner,
            "repo": repo,
            "repo_key": repo_key,
            "source_url": source_url,
            "full_name": None,
            "html_url": None,
            "description": None,
            "homepage": None,
            "private": None,
            "fork": None,
            "archived": None,
            "disabled": None,
            "is_template": None,
            "language": None,
            "default_branch": None,
            "size_kb": None,
            "stars": None,
            "forks": None,
            "watchers": None,
            "open_issues": None,
            "created_at": None,
            "updated_at": None,
            "pushed_at": None,
            "raw_json": None,
            "status_code": status_code,
            "error": error,
            "fetched_at": now,
        }

    return {
        "owner": owner,
        "repo": repo,
        "repo_key": repo_key,
        "source_url": source_url,
        "full_name": payload.get("full_name"),
        "html_url": payload.get("html_url"),
        "description": payload.get("description"),
        "homepage": payload.get("homepage"),
        "private": bool_to_int(payload.get("private")),
        "fork": bool_to_int(payload.get("fork")),
        "archived": bool_to_int(payload.get("archived")),
        "disabled": bool_to_int(payload.get("disabled")),
        "is_template": bool_to_int(payload.get("is_template")),
        "language": payload.get("language"),
        "default_branch": payload.get("default_branch"),
        "size_kb": payload.get("size"),
        "stars": payload.get("stargazers_count"),
        "forks": payload.get("forks_count"),
        "watchers": payload.get("watchers_count"),
        "open_issues": payload.get("open_issues_count"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "pushed_at": payload.get("pushed_at"),
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "status_code": status_code,
        "error": error,
        "fetched_at": now,
    }


def save_record(conn: sqlite3.Connection, table: str, record: dict[str, Any]) -> None:
    columns = list(record.keys())
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(columns)
    update_sql = ", ".join([f"{col}=excluded.{col}" for col in columns if col != "repo_key"])

    conn.execute(
        f"""
        INSERT INTO {table} ({column_sql})
        VALUES ({placeholders})
        ON CONFLICT(repo_key) DO UPDATE SET {update_sql};
        """,
        [record[col] for col in columns],
    )
    conn.commit()


def build_session(token: str | None) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": "hackathon-thesis-github-cache",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    return session


def fetch_repo(session: requests.Session, owner: str, repo: str, timeout: int) -> tuple[int, dict[str, Any] | None, str | None]:
    url = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        response = session.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return 0, None, str(exc)

    if response.status_code == 200:
        return response.status_code, response.json(), None

    error_message = None
    try:
        error_payload = response.json()
        error_message = error_payload.get("message")
    except Exception:
        error_message = response.text[:500]

    return response.status_code, None, error_message


def respect_rate_limit(response_status: int, sleep_seconds: float) -> None:
    """Pause between requests; use a longer pause for likely rate-limit responses."""
    if response_status in {403, 429}:
        time.sleep(max(60.0, sleep_seconds))
    elif sleep_seconds > 0:
        time.sleep(sleep_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect GitHub repository metadata into a SQLite cache.")
    parser.add_argument("--projects-csv", type=Path, default=Path("dataset/dataset/Projects Dataset/all_project_details.csv"))
    parser.add_argument("--github-column", default="GitHub link")
    parser.add_argument("--output-db", type=Path, default=Path("github_repo_cache.sqlite"))
    parser.add_argument("--table", default="repos_cache")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for testing.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between API requests.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--force-refresh", action="store_true", help="Refetch repositories already present in the cache.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.projects_csv.exists():
        raise FileNotFoundError(f"Projects CSV not found: {args.projects_csv}")

    token = os.getenv(args.token_env)
    if not token:
        print(
            f"Warning: {args.token_env} is not set. The script can run without it, "
            "but GitHub may rate-limit the run quickly.",
            file=sys.stderr,
        )

    projects = pd.read_csv(args.projects_csv)
    if args.github_column not in projects.columns:
        raise ValueError(
            f"Column {args.github_column!r} not found in {args.projects_csv}. "
            f"Available columns: {list(projects.columns)}"
        )

    repos = extract_unique_repositories(projects, args.github_column)
    if args.limit is not None:
        repos = repos.head(args.limit)

    args.output_db.parent.mkdir(parents=True, exist_ok=True)
    session = build_session(token)

    with sqlite3.connect(args.output_db) as conn:
        ensure_schema(conn, args.table)
        already_cached = existing_repo_keys(conn, args.table) if not args.force_refresh else set()

        total = len(repos)
        to_fetch = repos[~repos["repo_key"].isin(already_cached)].copy()

        print(f"Unique valid GitHub repositories found: {total:,}")
        print(f"Already cached: {len(already_cached):,}")
        print(f"To fetch: {len(to_fetch):,}")
        print(f"Writing cache to: {args.output_db}")

        for i, row in enumerate(to_fetch.itertuples(index=False), start=1):
            owner = row.owner
            repo = row.repo
            repo_key = row.repo_key
            source_url = row.source_url

            status_code, payload, error = fetch_repo(session, owner, repo, args.timeout)
            record = repo_payload_to_record(
                owner=owner,
                repo=repo,
                repo_key=repo_key,
                source_url=source_url,
                status_code=status_code,
                payload=payload,
                error=error,
            )
            save_record(conn, args.table, record)

            if i == 1 or i % 100 == 0 or status_code != 200:
                status = "ok" if status_code == 200 else f"status={status_code} error={error}"
                print(f"[{i:,}/{len(to_fetch):,}] {owner}/{repo}: {status}")

            respect_rate_limit(status_code, args.sleep)

    print("Done.")


if __name__ == "__main__":
    main()
