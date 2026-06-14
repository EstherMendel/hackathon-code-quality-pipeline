from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Error categorisation
# ---------------------------------------------------------------------------

def categorize_project_error(df: pd.DataFrame) -> pd.DataFrame:
    """Add a normalized project-level `error_category` column."""
    df = df.copy()

    if "error" not in df.columns:
        df["error"] = ""

    err = df["error"].fillna("").astype(str).str.strip()

    df["error_category"] = np.select(
        [
            err.eq(""),
            err.str.startswith("No commit exists before", na=False),
            err.str.contains(
                "Cannot prompt because user interactivity has been disabled.",
                na=False,
                regex=False,
            ),
            err.str.contains("Malformed input", na=False, regex=False),
            err.str.contains("invalid path", na=False, regex=False),
            err.str.contains("Filename too long", na=False, regex=False),
            err.str.contains("not a git repository", na=False, regex=False),
            err.str.startswith("Could not detect language", na=False),
            err.str.startswith("Could not detect python/javascript", na=False),
            err.str.contains("checkout failed: git timeout after", na=False, regex=False),
            err.str.startswith("timeout: processing ", na=False),
            err.str.startswith(
                "clone failed: clone failed: remote: Repository not found",
                na=False,
            ),
            err.str.startswith("metric extraction failed", na=False),
            err.str.startswith("All files failed to parse", na=False),
            err.str.startswith("Suspect all-zero metrics", na=False),
            err.str.startswith(
                "No analyzable python/javascript source files after exclusions",
                na=False,
            ),
            err.str.contains("Updating files", na=False, regex=False),
            err.str.contains("Could not resolve host: github.com", na=False, regex=False),
            err.str.contains(
                "already exists and is not an empty directory.",
                na=False,
                regex=False,
            ),
            err.str.contains("git timeout after 600s:", na=False, regex=False),
        ],
        [
            "Empty / no error message",
            "No commit before end event",
            "User interactivity disabled",
            "Malformed input",
            "Invalid path",
            "Filename too long",
            "Not a git repository",
            "Could not detect language",
            "Could not detect language",
            "Checkout failed: git timeout",
            "Timeout (exceeded max analysis time)",
            "Repository not found",
            "Metric extraction failed",
            "All files failed to parse",
            "Suspect all-zero metrics",
            "No analyzable source files after exclusions",
            "Updating files",
            "Could not resolve host: github.com",
            "Repository already exists on computer",
            "git timeout after 600s",
        ],
        default=err,
    )

    return df


# ---------------------------------------------------------------------------
# File-level failure classification helpers
# ---------------------------------------------------------------------------

def clean_str(x: Any) -> str:
    if pd.isna(x):
        return ""
    return str(x).strip()


def norm_path(path: Any) -> str:
    return clean_str(path).replace("\\", "/").lower()


def norm_lang(lang: Any) -> str:
    return clean_str(lang).lower()


def is_parse_failed_row(row: pd.Series) -> bool:
    """Return True when a file row failed parsing or has a parse error message."""
    parse_ok = row.get("parse_ok", np.nan)
    parse_error = clean_str(row.get("parse_error", ""))

    parse_ok_failed = False
    if pd.notna(parse_ok):
        try:
            parse_ok_failed = int(parse_ok) == 0
        except (TypeError, ValueError):
            parse_ok_failed = False

    return parse_ok_failed or parse_error != ""


def path_is_vendored_or_generated(rel_path: Any) -> bool:
    """Return True for paths that should not remove a repository from analysis."""
    path = norm_path(rel_path)
    patterns = [
        "/node_modules/",
        "/vendor/",
        "/vendors/",
        "/third_party/",
        "/third-party/",
        "/bower_components/",
        "/jspm_packages/",
        "/site-packages/",
        "/dist/",
        "/build/",
        "/generated/",
        "/gen/",
        "/coverage/",
        "/public/",
        "/static/",
        "/assets/",
        "/google-cloud-sdk/",
        "/dataconnect-generated/",
    ]
    return any(token in path for token in patterns)


def is_python2_failure(parse_error: Any, rel_path: Any = "", lang: Any = "") -> bool:
    """Return True for parse failures consistent with Python 2 syntax."""
    msg = clean_str(parse_error).lower()
    path = norm_path(rel_path)
    language = norm_lang(lang)

    python2_patterns = [
        r"\[py2_print\]",
        r"missing parentheses in call to 'print'",
        r"multiple exception types must be parenthesized",
    ]

    if any(re.search(pattern, msg, flags=re.IGNORECASE) for pattern in python2_patterns):
        return True

    if "/python2/" in path or "/lib2/" in path:
        return True

    if language == "python" and "raise " in msg and "," in msg and "invalid syntax" in msg:
        return True

    return False


def classify_failed_file(row: pd.Series) -> str:
    """Classify a failed file into an allowed or disallowed failure rule."""
    rel_path = row.get("rel_path", "")
    parse_error = row.get("parse_error", "")
    lang = row.get("lang", "")

    if path_is_vendored_or_generated(rel_path):
        return "allowed_failure_vendored_or_generated"

    if is_python2_failure(parse_error, rel_path, lang):
        return "allowed_failure_python2"

    return "disallowed_parse_failure"


# ---------------------------------------------------------------------------
# Filtering process
# ---------------------------------------------------------------------------

def build_error_filtered_dataset(
    df_projects: pd.DataFrame,
    df_file_metrics: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """Build included projects, audit table, and summary tables.

    Returns a dictionary with:
    - projects: filtered project table for the main analysis;
    - all_projects_with_filter_flags: full project table with decision columns;
    - project_filter_audit: compact audit table;
    - summary_primary: mutually exclusive project-level decisions;
    - summary_file_failure_details: file-level failure details;
    - included_allowed_failure_summary: allowed failures among retained projects;
    - summary_triggered_conditions: non-mutually-exclusive audit counts.
    """
    required_project_cols = {"project_uid"}
    required_file_cols = {"project_uid"}

    missing_project_cols = required_project_cols - set(df_projects.columns)
    missing_file_cols = required_file_cols - set(df_file_metrics.columns)

    if missing_project_cols:
        raise ValueError(f"df_projects is missing required columns: {sorted(missing_project_cols)}")
    if missing_file_cols:
        raise ValueError(f"df_file_metrics is missing required columns: {sorted(missing_file_cols)}")

    df_projects = categorize_project_error(df_projects)

    df_files = df_file_metrics.copy()

    if "parse_ok" not in df_files.columns:
        df_files["parse_ok"] = np.nan
    if "parse_error" not in df_files.columns:
        df_files["parse_error"] = ""

    df_files["is_failed"] = df_files.apply(is_parse_failed_row, axis=1)
    df_files["is_parse_ok"] = (
        pd.to_numeric(df_files["parse_ok"], errors="coerce")
        .fillna(0)
        .eq(1)
    )

    df_fail_files = df_files[df_files["is_failed"]].copy()
    if df_fail_files.empty:
        df_fail_files = pd.DataFrame(
            columns=list(df_files.columns) + ["failure_rule"]
        )
    else:
        df_fail_files["failure_rule"] = df_fail_files.apply(classify_failed_file, axis=1)

    expected_failure_cols = [
        "allowed_failure_vendored_or_generated",
        "allowed_failure_python2",
        "disallowed_parse_failure",
    ]

    if df_fail_files.empty:
        fail_summary = pd.DataFrame(columns=["project_uid", *expected_failure_cols])
    else:
        fail_summary = (
            df_fail_files
            .groupby(["project_uid", "failure_rule"], dropna=False)
            .size()
            .unstack(fill_value=0)
            .reset_index()
        )

    for col in expected_failure_cols:
        if col not in fail_summary.columns:
            fail_summary[col] = 0

    fail_summary["failed_file_rows_total"] = fail_summary[expected_failure_cols].sum(axis=1)

    parse_summary = (
        df_files
        .groupby("project_uid", dropna=False)
        .agg(
            total_file_rows=("project_uid", "size"),
            parse_ok_files_recalc=("is_parse_ok", "sum"),
            failed_file_rows_recalc=("is_failed", "sum"),
        )
        .reset_index()
    )

    df = df_projects.copy()
    df = df.merge(parse_summary, on="project_uid", how="left")
    df = df.merge(fail_summary, on="project_uid", how="left")

    numeric_fill_zero_cols = [
        "total_file_rows",
        "parse_ok_files_recalc",
        "failed_file_rows_recalc",
        "allowed_failure_vendored_or_generated",
        "allowed_failure_python2",
        "disallowed_parse_failure",
        "failed_file_rows_total",
    ]

    for col in numeric_fill_zero_cols:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    if "status" not in df.columns:
        df["status"] = ""
    if "suspect_all_zero" not in df.columns:
        df["suspect_all_zero"] = 0

    status = df["status"].fillna("").astype(str)
    error_category = df["error_category"].fillna("").astype(str)
    suspect_all_zero = pd.to_numeric(df["suspect_all_zero"], errors="coerce").fillna(0)

    fatal_error_categories = {
        "Suspect all-zero metrics",
        "No analyzable source files after exclusions",
    }

    df["flag_suspect_all_zero"] = suspect_all_zero.eq(1)

    df["flag_fatal_no_analyzable_source_files"] = (
        status.isin(["error", "error-permanent"])
        & error_category.eq("No analyzable source files after exclusions")
    )

    df["flag_fatal_other_named_fatal_error"] = (
        status.isin(["error", "error-permanent"])
        & error_category.isin(fatal_error_categories)
        & ~df["flag_fatal_no_analyzable_source_files"]
        & ~df["flag_suspect_all_zero"]
    )

    df["flag_zero_parseable_files"] = df["parse_ok_files_recalc"].eq(0)
    df["flag_disallowed_parse_failures_present"] = df["disallowed_parse_failure"].gt(0)
    df["flag_only_allowed_failures"] = (
        df["failed_file_rows_total"].gt(0)
        & df["disallowed_parse_failure"].eq(0)
    )

    df["primary_filter_decision"] = np.select(
        [
            df["flag_suspect_all_zero"],
            df["flag_fatal_no_analyzable_source_files"],
            df["flag_fatal_other_named_fatal_error"],
            df["flag_zero_parseable_files"],
            df["flag_disallowed_parse_failures_present"],
        ],
        [
            "exclude_suspect_all_zero_metrics",
            "exclude_no_analyzable_source_files_after_exclusions",
            "exclude_other_fatal_technical_error",
            "exclude_zero_parseable_files",
            "exclude_disallowed_parse_failures_present",
        ],
        default="include",
    )

    df["include_main"] = df["primary_filter_decision"].eq("include")

    decision_label_map = {
        "include": "Included",
        "exclude_suspect_all_zero_metrics": "Excluded: suspect all-zero metrics",
        "exclude_no_analyzable_source_files_after_exclusions": (
            "Excluded: no analyzable source files after exclusions"
        ),
        "exclude_other_fatal_technical_error": "Excluded: other fatal technical error",
        "exclude_zero_parseable_files": "Excluded: zero parseable files",
        "exclude_disallowed_parse_failures_present": (
            "Excluded: disallowed parse failures present"
        ),
    }

    df["primary_filter_decision_label"] = df["primary_filter_decision"].map(decision_label_map)

    def collect_all_triggered_reasons(row: pd.Series) -> list[str]:
        reasons = []
        if row["flag_suspect_all_zero"]:
            reasons.append("suspect_all_zero_metrics")
        if row["flag_fatal_no_analyzable_source_files"]:
            reasons.append("no_analyzable_source_files_after_exclusions")
        if row["flag_fatal_other_named_fatal_error"]:
            reasons.append("other_fatal_technical_error")
        if row["flag_zero_parseable_files"]:
            reasons.append("zero_parseable_files")
        if row["flag_disallowed_parse_failures_present"]:
            reasons.append("disallowed_parse_failures_present")
        return reasons

    df["all_triggered_filter_reasons"] = df.apply(collect_all_triggered_reasons, axis=1)
    df["n_triggered_filter_reasons"] = df["all_triggered_filter_reasons"].str.len()

    df_included = df[df["include_main"]].copy()

    summary_primary = (
        df["primary_filter_decision_label"]
        .value_counts(dropna=False)
        .rename_axis("decision")
        .reset_index(name="n_projects")
    )

    decision_order = [
        "Included",
        "Excluded: suspect all-zero metrics",
        "Excluded: no analyzable source files after exclusions",
        "Excluded: other fatal technical error",
        "Excluded: zero parseable files",
        "Excluded: disallowed parse failures present",
    ]
    summary_primary["sort_order"] = summary_primary["decision"].map(
        {label: index for index, label in enumerate(decision_order)}
    )
    summary_primary = (
        summary_primary
        .sort_values(["sort_order", "decision"])
        .drop(columns="sort_order")
        .reset_index(drop=True)
    )

    summary_file_failure_details = pd.DataFrame(
        {
            "detail_category": [
                "Failed files classified as allowed: vendored/generated",
                "Failed files classified as allowed: Python 2",
                "Failed files classified as disallowed parse failures",
            ],
            "n_file_rows": [
                int(df["allowed_failure_vendored_or_generated"].sum()),
                int(df["allowed_failure_python2"].sum()),
                int(df["disallowed_parse_failure"].sum()),
            ],
        }
    )

    included_allowed_failure_summary = pd.DataFrame(
        {
            "detail_category": [
                "Included projects with any allowed vendored/generated failures",
                "Included projects with any allowed Python 2 failures",
                "Included projects with only allowed failures",
            ],
            "n_projects": [
                int((df_included["allowed_failure_vendored_or_generated"] > 0).sum()),
                int((df_included["allowed_failure_python2"] > 0).sum()),
                int(df_included["flag_only_allowed_failures"].sum()),
            ],
        }
    )

    summary_triggered_conditions = pd.DataFrame(
        {
            "triggered_condition": [
                "suspect_all_zero_metrics",
                "no_analyzable_source_files_after_exclusions",
                "other_fatal_technical_error",
                "zero_parseable_files",
                "disallowed_parse_failures_present",
            ],
            "n_projects_triggering_condition": [
                int(df["flag_suspect_all_zero"].sum()),
                int(df["flag_fatal_no_analyzable_source_files"].sum()),
                int(df["flag_fatal_other_named_fatal_error"].sum()),
                int(df["flag_zero_parseable_files"].sum()),
                int(df["flag_disallowed_parse_failures_present"].sum()),
            ],
        }
    )

    project_audit_cols = [
        "project_uid",
        "primary_filter_decision",
        "primary_filter_decision_label",
        "include_main",
        "status",
        "error_category",
        "total_file_rows",
        "parse_ok_files_recalc",
        "failed_file_rows_recalc",
        "allowed_failure_vendored_or_generated",
        "allowed_failure_python2",
        "disallowed_parse_failure",
        "flag_suspect_all_zero",
        "flag_fatal_no_analyzable_source_files",
        "flag_fatal_other_named_fatal_error",
        "flag_zero_parseable_files",
        "flag_disallowed_parse_failures_present",
        "flag_only_allowed_failures",
        "all_triggered_filter_reasons",
    ]
    project_audit_cols = [col for col in project_audit_cols if col in df.columns]
    df_project_filter_audit = df[project_audit_cols].copy()

    return {
        "projects": df_included,
        "all_projects_with_filter_flags": df,
        "project_filter_audit": df_project_filter_audit,
        "summary_primary": summary_primary,
        "summary_file_failure_details": summary_file_failure_details,
        "included_allowed_failure_summary": included_allowed_failure_summary,
        "summary_triggered_conditions": summary_triggered_conditions,
    }


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def read_sqlite_table(db_path: str | Path, table_name: str) -> pd.DataFrame:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    with sqlite3.connect(db_path) as conn:
        return pd.read_sql(f"SELECT * FROM {table_name}", conn)


def make_sql_friendly(df: pd.DataFrame) -> pd.DataFrame:
    """Convert list/dict values to JSON strings before SQLite export."""
    df = df.copy()
    for col in df.columns:
        if df[col].map(lambda value: isinstance(value, (list, dict))).any():
            df[col] = df[col].map(
                lambda value: json.dumps(value) if isinstance(value, (list, dict)) else value
            )
    return df


def save_outputs_to_sqlite(outputs: dict[str, pd.DataFrame], output_db: str | Path) -> None:
    output_db = Path(output_db)
    output_db.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(output_db) as conn:
        for table_name, table_df in outputs.items():
            make_sql_friendly(table_df).to_sql(
                table_name,
                conn,
                if_exists="replace",
                index=False,
            )


def run_error_filtering(
    projects_db: str | Path,
    file_metrics_db: str | Path,
    output_db: str | Path,
    projects_table: str = "projects",
    file_metrics_table: str = "file_metrics",
) -> dict[str, pd.DataFrame]:
    df_projects = read_sqlite_table(projects_db, projects_table)
    df_file_metrics = read_sqlite_table(file_metrics_db, file_metrics_table)

    outputs = build_error_filtered_dataset(df_projects, df_file_metrics)
    save_outputs_to_sqlite(outputs, output_db)

    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the error-filtered thesis project dataset."
    )
    parser.add_argument(
        "--projects-db",
        default="metrics_para/Data/dataset_filtered_v2.db",
        help="SQLite database containing the project-level `projects` table.",
    )
    parser.add_argument(
        "--file-metrics-db",
        default="metrics_para/Data/metrics_files.db",
        help="SQLite database containing the file-level `file_metrics` table.",
    )
    parser.add_argument(
        "--output-db",
        default="no_errors.db",
        help="Output SQLite database for the filtered dataset and audit tables.",
    )
    parser.add_argument(
        "--projects-table",
        default="projects",
        help="Name of the project-level table in --projects-db.",
    )
    parser.add_argument(
        "--file-metrics-table",
        default="file_metrics",
        help="Name of the file-level table in --file-metrics-db.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outputs = run_error_filtering(
        projects_db=args.projects_db,
        file_metrics_db=args.file_metrics_db,
        output_db=args.output_db,
        projects_table=args.projects_table,
        file_metrics_table=args.file_metrics_table,
    )

    n_original = len(outputs["all_projects_with_filter_flags"])
    n_included = len(outputs["projects"])
    n_excluded = n_original - n_included
    retention_rate = n_included / n_original if n_original else 0

    print(f"Saved filtered dataset and audit tables to: {args.output_db}")
    print(f"Original projects: {n_original}")
    print(f"Included projects: {n_included}")
    print(f"Excluded projects: {n_excluded}")
    print(f"Retention rate: {retention_rate:.4f}")


if __name__ == "__main__":
    main()
