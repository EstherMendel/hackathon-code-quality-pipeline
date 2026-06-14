#!/usr/bin/env python3
"""
Filter the GitHub-enriched Devpost project dataset for the analysis sample.

Input:
  - SQLite database created by `setup_dataset_github_enrichment.py`
  - Required table: `projects`

Outputs:
  - Filtered SQLite database, by default `data_filtered.db`
  - Main table: `projects`
  - Audit table: `filter_audit`
  - Optional sample database: `data_filtered_sample.db`

This script applies the dataset filtering step only. It assumes that the setup and
GitHub enrichment step has already been run.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd


GITHUB_REPO_URL_RE = re.compile(
    r"""^https?://github\.com/
        (?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/
        (?P<repo>[A-Za-z0-9._-]+)
        (?:\.git)?/?$
    """,
    re.X | re.IGNORECASE,
)


def extract_urls_maybe_list(value: Any) -> list[str]:
    if pd.isna(value):
        return []
    if isinstance(value, list):
        return [str(url).strip() for url in value if str(url).strip()]

    text = str(value).strip()
    if (text.startswith("[") and text.endswith("]")) or (text.startswith("(") and text.endswith(")")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(url).strip() for url in parsed if str(url).strip()]
        except Exception:
            pass
    return [part for part in re.split(r"[,\s|]+", text) if part]


def split_clean_and_invalid_urls(value: Any) -> tuple[list[str], list[str]]:
    clean: list[str] = []
    invalid: list[str] = []
    for url in extract_urls_maybe_list(value):
        url = str(url).strip()
        if not url:
            continue
        if GITHUB_REPO_URL_RE.match(url):
            clean.append(url)
        else:
            invalid.append(url)
    return clean, invalid


def to_bool_mask(series: pd.Series) -> pd.Series:
    """Normalize booleans that may have been stored as bools, 0/1, or strings."""
    return series.astype(str).str.lower().isin(["true", "1", "yes"])


def apply_step(df: pd.DataFrame, audit: list[dict[str, Any]], step: str, mask: pd.Series) -> pd.DataFrame:
    before = len(df)
    out = df[mask].copy()
    audit.append({
        "step": step,
        "rows_before": before,
        "rows_after": len(out),
        "rows_removed": before - len(out),
    })
    return out


def cochran_sample_size(population_size: int, margin_error: float = 0.05, confidence: float = 0.95, p: float = 0.5) -> int:
    """Finite-population Cochran sample size using common z-values."""
    z_lookup = {0.90: 1.644854, 0.95: 1.959964, 0.99: 2.575829}
    z = z_lookup.get(round(confidence, 2), 1.959964)
    q = 1 - p
    n0 = (z**2 * p * q) / (margin_error**2)
    n = n0 / (1 + (n0 - 1) / population_size)
    return min(population_size, math.ceil(n))


def make_sql_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        out[col] = out[col].apply(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (list, dict)) else x)
    return out


def filter_projects(projects: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = projects.copy()
    audit: list[dict[str, Any]] = []
    audit.append({"step": "start", "rows_before": len(df), "rows_after": len(df), "rows_removed": 0})

    df = apply_step(
        df,
        audit,
        "github_link_contains_github_com",
        df["GitHub link"].astype(str).str.contains("github.com", na=False),
    )

    url_parts = df["GitHub link"].map(split_clean_and_invalid_urls).apply(pd.Series)
    df["GitHub_valid"] = url_parts[0]
    df["GitHub_invalid"] = url_parts[1]
    df = apply_step(
        df,
        audit,
        "exactly_one_clean_repository_url",
        df["GitHub_valid"].apply(lambda urls: isinstance(urls, list) and len(urls) == 1),
    )

    df = apply_step(df, audit, "project_uid_available", df["UID"].notna())

    df["participant_count"] = pd.to_numeric(df["participant_count"], errors="coerce").fillna(0)
    df = apply_step(df, audit, "at_least_two_participants", df["participant_count"] >= 2)

    total_participants_per_hackathon = df.groupby("Hackathon uid")["participant_count"].sum()
    hackathons_with_10plus = total_participants_per_hackathon[total_participants_per_hackathon >= 10].index
    df = apply_step(
        df,
        audit,
        "hackathon_has_at_least_ten_participants",
        df["Hackathon uid"].isin(hackathons_with_10plus),
    )

    projects_per_hackathon = df.groupby("Hackathon uid")["project_uid"].nunique()
    hackathons_with_3plus_projects = projects_per_hackathon[projects_per_hackathon >= 3].index
    df = apply_step(
        df,
        audit,
        "hackathon_has_at_least_three_projects",
        df["Hackathon uid"].isin(hackathons_with_3plus_projects),
    )

    if "private" in df.columns:
        private_numeric = pd.to_numeric(df["private"], errors="coerce")
        private_mask = private_numeric.eq(0) | df["private"].astype(str).str.lower().isin(["false", "0"])
        df = apply_step(df, audit, "repository_is_public", private_mask)

    df["size_kb"] = pd.to_numeric(df["size_kb"], errors="coerce")
    df = apply_step(df, audit, "repository_size_above_zero", df["size_kb"] > 0)

    df["days_hackathon_to_creation"] = pd.to_numeric(df["days_hackathon_to_creation"], errors="coerce")
    df = apply_step(
        df,
        audit,
        "repository_created_on_or_after_hackathon_start",
        df["days_hackathon_to_creation"] >= 0,
    )

    df = apply_step(
        df,
        audit,
        "language_is_python_javascript_or_typescript",
        df["language"].isin(["Python", "JavaScript", "TypeScript"]),
    )

    return df, pd.DataFrame(audit)


def save_filtered_database(filtered: pd.DataFrame, audit: pd.DataFrame, output_db: Path) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output_db) as conn:
        make_sql_safe(filtered).to_sql("projects", conn, if_exists="replace", index=False)
        audit.to_sql("filter_audit", conn, if_exists="replace", index=False)
        pd.DataFrame([{"table": "projects", "rows": len(filtered)}]).to_sql(
            "dataset_summary", conn, if_exists="replace", index=False
        )


def save_sample_database(filtered: pd.DataFrame, output_db: Path, margin_error: float, confidence: float, seed: int) -> int:
    sample_size = cochran_sample_size(len(filtered), margin_error=margin_error, confidence=confidence)
    sample = filtered.sample(sample_size, random_state=seed)
    output_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output_db) as conn:
        make_sql_safe(sample).to_sql("projects", conn, if_exists="replace", index=False)
        pd.DataFrame([{"table": "projects", "rows": len(sample)}]).to_sql(
            "dataset_summary", conn, if_exists="replace", index=False
        )
    return sample_size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Filter the enriched Devpost-GitHub dataset for analysis.")
    parser.add_argument("--input-db", type=Path, default=Path("dataset_github_enriched.db"))
    parser.add_argument("--output-db", type=Path, default=Path("data_filtered.db"))
    parser.add_argument("--sample-output-db", type=Path, default=None)
    parser.add_argument("--sample-margin-error", type=float, default=0.05)
    parser.add_argument("--sample-confidence", type=float, default=0.95)
    parser.add_argument("--sample-seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with sqlite3.connect(args.input_db) as conn:
        projects = pd.read_sql_query("SELECT * FROM projects", conn)

    filtered, audit = filter_projects(projects)
    save_filtered_database(filtered, audit, args.output_db)

    print(f"Saved filtered dataset to {args.output_db}")
    print(f"Rows before filtering: {len(projects):,}")
    print(f"Rows after filtering: {len(filtered):,}")

    if args.sample_output_db is not None:
        sample_size = save_sample_database(
            filtered,
            args.sample_output_db,
            margin_error=args.sample_margin_error,
            confidence=args.sample_confidence,
            seed=args.sample_seed,
        )
        print(f"Saved sample database to {args.sample_output_db} ({sample_size:,} rows)")


if __name__ == "__main__":
    main()
