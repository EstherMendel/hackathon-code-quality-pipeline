#!/usr/bin/env python3
"""
Set up the Devpost project dataset and enrich it with cached GitHub metadata.

Inputs:
  - Devpost hackathon CSV
  - Devpost project CSV
  - Devpost participant CSV
  - SQLite GitHub repository cache, expected to contain a `repos_cache` table

Output:
  - SQLite database, by default `dataset_github_enriched.db`
  - Main table: `projects`
  - Supporting tables: `hackathons`, `participants`, `github_cache`

This script only constructs and enriches the dataset. It does not apply the final
analysis filters; use `filter_dataset.py` for that step.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import pandas as pd


_DASH = r"[–—-]"
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    start=1,
)}


def month_to_number(value: str) -> int | None:
    value = value.strip()
    return _MONTHS.get(value[:3].title())


def parse_date_range(value: Any) -> tuple[pd.Timestamp | pd.NaT, pd.Timestamp | pd.NaT]:
    """Parse Devpost date ranges such as 'Jan 1 - Jan 3, 2024'."""
    if not isinstance(value, str):
        return pd.NaT, pd.NaT

    text = " ".join(value.replace("\u00a0", " ").split())

    match = re.fullmatch(
        rf"([A-Za-z]+)\s+(\d{{1,2}})\s*{_DASH}\s*([A-Za-z]+)\s+(\d{{1,2}}),\s*(\d{{4}})",
        text,
    )
    if match:
        start_month, start_day, end_month, end_day, year = match.groups()
        start_month_number = month_to_number(start_month)
        end_month_number = month_to_number(end_month)
        if start_month_number is None or end_month_number is None:
            return pd.NaT, pd.NaT
        year = int(year)
        start_year = year if start_month_number <= end_month_number else year - 1
        try:
            return (
                pd.Timestamp(year=start_year, month=start_month_number, day=int(start_day)),
                pd.Timestamp(year=year, month=end_month_number, day=int(end_day)),
            )
        except Exception:
            return pd.NaT, pd.NaT

    match = re.fullmatch(
        rf"([A-Za-z]+)\s+(\d{{1,2}})\s*{_DASH}\s*(\d{{1,2}}),\s*(\d{{4}})",
        text,
    )
    if match:
        month, start_day, end_day, year = match.groups()
        month_number = month_to_number(month)
        if month_number is None:
            return pd.NaT, pd.NaT
        try:
            year = int(year)
            return (
                pd.Timestamp(year=year, month=month_number, day=int(start_day)),
                pd.Timestamp(year=year, month=month_number, day=int(end_day)),
            )
        except Exception:
            return pd.NaT, pd.NaT

    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if match:
        month, day, year = match.groups()
        month_number = month_to_number(month)
        if month_number is None:
            return pd.NaT, pd.NaT
        try:
            timestamp = pd.Timestamp(year=int(year), month=month_number, day=int(day))
            return timestamp, timestamp
        except Exception:
            return pd.NaT, pd.NaT

    return pd.NaT, pd.NaT


def add_hackathon_dates(df: pd.DataFrame, date_col: str = "Date") -> pd.DataFrame:
    parsed = df[date_col].apply(parse_date_range).apply(pd.Series)
    parsed.columns = ["Start_Date", "End_Date"]
    parsed["Start_Date"] = pd.to_datetime(parsed["Start_Date"], errors="coerce")
    parsed["End_Date"] = pd.to_datetime(parsed["End_Date"], errors="coerce")

    out = pd.concat([df.copy(), parsed], axis=1)
    out["Timespan_days"] = (out["End_Date"] - out["Start_Date"]).dt.days + 1
    return out


def parse_list_cell(value: Any) -> Any:
    """Safely parse list-like strings while leaving other values unchanged."""
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    return value


def split_tag_column(df: pd.DataFrame, col: str, sep: str = ",") -> pd.DataFrame:
    out = df.copy()
    if col in out.columns:
        out[col] = (
            out[col]
            .fillna("")
            .astype(str)
            .apply(lambda x: [tag.strip() for tag in x.split(sep) if tag.strip()])
        )
    return out


def detect_currency(value: Any) -> str:
    if pd.isna(value):
        return "UNKNOWN"
    text = str(value).upper()
    if "USD" in text or "$" in text:
        return "USD"
    if "EUR" in text or "€" in text:
        return "EUR"
    if "GBP" in text or "£" in text:
        return "GBP"
    if "INR" in text or "₹" in text:
        return "INR"
    if "CAD" in text:
        return "CAD"
    if "AUD" in text:
        return "AUD"
    if "SGD" in text:
        return "SGD"
    if "JPY" in text or "¥" in text:
        return "JPY"
    return "UNKNOWN"


def extract_amount(value: Any) -> float:
    if pd.isna(value):
        return np.nan
    numbers = re.findall(r"[\d,]+(?:\.\d+)?", str(value).replace(" ", ""))
    if not numbers:
        return np.nan
    return max(float(number.replace(",", "")) for number in numbers)


def add_prize_usd(df: pd.DataFrame, prize_col: str = "Prize Money") -> pd.DataFrame:
    conversion_rates = {
        "USD": 1.0,
        "EUR": 1.08,
        "GBP": 1.27,
        "INR": 0.012,
        "CAD": 0.74,
        "AUD": 0.66,
        "SGD": 0.73,
        "JPY": 0.0067,
        "UNKNOWN": np.nan,
    }
    out = df.copy()
    if prize_col in out.columns:
        out["prize_amount"] = out[prize_col].apply(extract_amount)
        out["prize_currency"] = out[prize_col].apply(detect_currency)
        out["prize_usd"] = out.apply(
            lambda row: row["prize_amount"] * conversion_rates.get(row["prize_currency"], np.nan),
            axis=1,
        )
    return out


_GITHUB_REPO_RE = re.compile(
    r"""^
        (?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})
        /
        (?P<repo>[A-Za-z0-9._-]+)
        $
    """,
    re.X,
)


def extract_urls_maybe_list(value: Any) -> list[str]:
    if pd.isna(value):
        return []
    if isinstance(value, list):
        return [str(url).strip() for url in value if str(url).strip()]

    text = str(value).strip()
    if (text.startswith("[") and text.endswith("]")) or (text.startswith("(") and text.endswith(")")):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)):
                return [str(url).strip() for url in parsed if str(url).strip()]
        except Exception:
            pass

    return [part for part in re.split(r"[,\s|]+", text) if part]


def owner_repo_from_github_url(url: str) -> tuple[str | None, str | None]:
    try:
        parsed = urlparse(url)
        if "github.com" not in (parsed.netloc or ""):
            return None, None
        parts = [part for part in (parsed.path or "").split("/") if part]
        if len(parts) < 2:
            return None, None
        owner, repo = parts[0], parts[1]
        repo = repo[:-4] if repo.endswith(".git") else repo
        if not _GITHUB_REPO_RE.match(f"{owner}/{repo}"):
            return None, None
        return owner, repo
    except Exception:
        return None, None


def first_owner_repo_from_cell(value: Any) -> tuple[str | None, str | None]:
    for url in extract_urls_maybe_list(value):
        owner, repo = owner_repo_from_github_url(url)
        if owner and repo:
            return owner, repo
    return None, None


def merge_projects_with_github_metadata(
    projects: pd.DataFrame,
    github_cache: pd.DataFrame,
    github_col: str = "GitHub link",
) -> pd.DataFrame:
    owners_repos = projects.get(github_col, pd.Series([None] * len(projects))).map(first_owner_repo_from_cell)
    owners, repos = zip(*owners_repos)

    projects = projects.copy()
    projects["owner"] = owners
    projects["repo"] = repos

    github_cache = github_cache.copy()
    if "owner" not in github_cache.columns or "repo" not in github_cache.columns:
        if "full_name" in github_cache.columns:
            split = github_cache["full_name"].astype(str).str.split("/", n=1, expand=True)
            github_cache["owner"] = split[0]
            github_cache["repo"] = split[1]
        elif "html_url" in github_cache.columns:
            github_cache[["owner", "repo"]] = github_cache["html_url"].map(owner_repo_from_github_url).apply(pd.Series)

    merged = projects.merge(github_cache, on=["owner", "repo"], how="left", suffixes=("", "_gh"))
    for col in ["stars", "forks", "open_issues", "size_kb"]:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")
            if col != "size_kb":
                merged[col] = merged[col].astype("Int64")
    return merged


def decode_raw_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if pd.isna(value):
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def extract_extra_github_fields(repo_json: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_issues": repo_json.get("has_issues"),
        "has_projects": repo_json.get("has_projects"),
        "has_downloads": repo_json.get("has_downloads"),
        "has_wiki": repo_json.get("has_wiki"),
        "has_pages": repo_json.get("has_pages"),
        "has_discussions": repo_json.get("has_discussions"),
        "allow_forking": repo_json.get("allow_forking"),
        "is_template": repo_json.get("is_template"),
        "web_commit_signoff_required": repo_json.get("web_commit_signoff_required"),
        "archived": repo_json.get("archived"),
        "disabled": repo_json.get("disabled"),
        "watchers_count": repo_json.get("watchers_count"),
        "subscribers_count": repo_json.get("subscribers_count"),
        "network_count": repo_json.get("network_count"),
        "visibility": repo_json.get("visibility"),
        "license_name": repo_json.get("license", {}).get("name") if repo_json.get("license") else None,
        "license_spdx_id": repo_json.get("license", {}).get("spdx_id") if repo_json.get("license") else None,
    }


def add_extra_github_fields(projects: pd.DataFrame) -> pd.DataFrame:
    out = projects.copy()
    if "raw_json" not in out.columns:
        return out
    out["raw_json"] = out["raw_json"].apply(decode_raw_json)
    extra = pd.DataFrame([extract_extra_github_fields(repo) for repo in out["raw_json"]])
    extra = extra[[col for col in extra.columns if col not in out.columns]]
    return pd.concat([out.reset_index(drop=True), extra], axis=1)


def add_hackathon_fields(projects: pd.DataFrame, hackathons: pd.DataFrame) -> pd.DataFrame:
    projects = projects.copy()
    hackathons = hackathons.copy()
    projects["Hackathon uid"] = projects["Hackathon uid"].astype(str)
    hackathons["UID"] = hackathons["UID"].astype(str)

    if hackathons["UID"].duplicated().any():
        raise ValueError("Hackathon UID values are not unique; cannot safely merge hackathon fields.")

    return projects.merge(
        hackathons,
        how="left",
        left_on="Hackathon uid",
        right_on="UID",
        suffixes=("", "_hack"),
    )


def add_participant_counts(projects: pd.DataFrame, participants: pd.DataFrame) -> pd.DataFrame:
    out = projects.copy()
    participants = participants.copy()

    out["project_uid"] = out["project_uid"].astype(str).str.strip()
    participants["Project_uid"] = participants["Project_uid"].astype(str).str.strip()

    participant_counts = (
        participants.groupby("Project_uid")["Participant_uid"]
        .nunique()
        .reset_index(name="participant_count")
        .rename(columns={"Project_uid": "project_uid"})
    )

    out = out.drop(
        columns=[col for col in ["participant_count", "participant_count_x", "participant_count_y"] if col in out.columns],
        errors="ignore",
    )
    out = out.merge(participant_counts, on="project_uid", how="left")
    out["participant_count"] = out["participant_count"].fillna(0).astype(int)
    return out


def days_between(later: pd.Series, earlier: pd.Series) -> pd.Series:
    delta = later - earlier
    out = pd.Series(np.nan, index=delta.index)
    mask = delta.notna()
    out.loc[mask] = np.round(delta[mask] / pd.Timedelta(days=1))
    return out.astype("Int64")


def add_repository_timing_fields(projects: pd.DataFrame) -> pd.DataFrame:
    out = projects.copy()
    for col in ["created_at", "updated_at", "pushed_at", "Start_Date", "Date", "End_Date"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce", utc=True)

    out["final_change_at"] = out[["pushed_at", "updated_at"]].max(axis=1)
    out["hackathon_date"] = out["Start_Date"].where(out["Start_Date"].notna(), out.get("Date"))
    out["days_creation_to_last_change"] = days_between(out["final_change_at"], out["created_at"])
    out["days_hackathon_to_creation"] = days_between(out["created_at"], out["hackathon_date"])
    out["days_hackathon_to_last_change"] = days_between(out["final_change_at"], out["hackathon_date"])
    return out


def make_sql_safe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        out[col] = out[col].apply(lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (list, dict)) else x)
    return out


def load_github_cache(db_path: Path, table: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(f"SELECT * FROM {table}", conn)


def build_dataset(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    hackathons = pd.read_csv(args.hackathons_csv)
    projects = pd.read_csv(args.projects_csv)
    participants = pd.read_csv(args.participants_csv)
    github_cache = load_github_cache(args.github_cache_db, args.github_cache_table)

    hackathons = add_hackathon_dates(hackathons, "Date")
    if "Tags" in hackathons.columns:
        hackathons["Tags"] = hackathons["Tags"].apply(parse_list_cell)
    hackathons = add_prize_usd(hackathons, "Prize Money")

    projects = split_tag_column(projects, "Built with (Tools used/ tags)", sep=",")
    projects = merge_projects_with_github_metadata(projects, github_cache, github_col="GitHub link")
    projects = add_extra_github_fields(projects)
    projects = add_hackathon_fields(projects, hackathons)
    projects = add_participant_counts(projects, participants)
    projects = add_repository_timing_fields(projects)

    return projects, hackathons, participants, github_cache


def save_database(
    projects: pd.DataFrame,
    hackathons: pd.DataFrame,
    participants: pd.DataFrame,
    github_cache: pd.DataFrame,
    output_db: Path,
) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output_db) as conn:
        make_sql_safe(projects).to_sql("projects", conn, if_exists="replace", index=False)
        make_sql_safe(hackathons).to_sql("hackathons", conn, if_exists="replace", index=False)
        make_sql_safe(participants).to_sql("participants", conn, if_exists="replace", index=False)
        make_sql_safe(github_cache).to_sql("github_cache", conn, if_exists="replace", index=False)

        summary = pd.DataFrame([
            {"table": "projects", "rows": len(projects)},
            {"table": "hackathons", "rows": len(hackathons)},
            {"table": "participants", "rows": len(participants)},
            {"table": "github_cache", "rows": len(github_cache)},
        ])
        summary.to_sql("dataset_summary", conn, if_exists="replace", index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up and GitHub-enrich the Devpost hackathon project dataset.")
    parser.add_argument("--hackathons-csv", type=Path, default=Path("dataset/dataset/Hackathon Dataset/devpost_hackathon_data.csv"))
    parser.add_argument("--projects-csv", type=Path, default=Path("dataset/dataset/Projects Dataset/all_project_details.csv"))
    parser.add_argument("--participants-csv", type=Path, default=Path("dataset/dataset/Participant Dataset/Final/participants_details.csv"))
    parser.add_argument("--github-cache-db", type=Path, default=Path("github_repo_cache.sqlite"))
    parser.add_argument("--github-cache-table", default="repos_cache")
    parser.add_argument("--output-db", type=Path, default=Path("dataset_github_enriched.db"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    projects, hackathons, participants, github_cache = build_dataset(args)
    save_database(projects, hackathons, participants, github_cache, args.output_db)
    print(f"Saved enriched dataset to {args.output_db}")
    print(f"Projects: {len(projects):,}")


if __name__ == "__main__":
    main()
