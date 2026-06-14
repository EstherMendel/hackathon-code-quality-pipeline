# main.py
"""
Entry point for running the repository metric extraction pipeline.

By default, this runs the core extraction without the elbow-based continuation
window. The elbow extraction can be enabled in run_pipeline() when needed.
"""

import sqlite3
from pathlib import Path
import pandas as pd

from config import DB_PATH
from db import load_projects_table
from pipeline import run_pipeline


# ----------------------------
# Data loading
# ----------------------------
def load_projects_from_db(db_path: str | Path = DB_PATH) -> pd.DataFrame:
    """Load project rows used as input for the pipeline."""
    conn = sqlite3.connect(str(db_path))
    try:
        return load_projects_table(conn)
    finally:
        conn.close()


# ----------------------------
# Optional elbow retry helpers
# ----------------------------
def load_elbow_github_error_projects(db_path: str | Path = DB_PATH) -> pd.DataFrame:
    """Load projects where elbow extraction failed because GitHub could not be reached."""
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT *
            FROM projects
            WHERE project_uid IS NOT NULL
              AND TRIM(project_uid) <> ''
              AND github_link IS NOT NULL
              AND TRIM(github_link) <> ''
              AND elbow_status = 'error'
              AND elbow_error LIKE '%Could not resolve host: github.com%'
            ORDER BY project_uid
            """,
            conn,
        )

        if df.empty:
            return df

        df["project_uid"] = df["project_uid"].astype(str).str.strip()
        return df.drop_duplicates("project_uid", keep="last").reset_index(drop=True)

    finally:
        conn.close()


def clear_previous_elbow_error_flags(df_projects: pd.DataFrame) -> None:
    """
    Clear failed elbow fields before rerunning elbow extraction.

    This only resets elbow-related columns and does not touch the core metrics.
    """
    if df_projects.empty:
        return

    project_uids = df_projects["project_uid"].dropna().astype(str).str.strip().tolist()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        placeholders = ",".join(["?"] * len(project_uids))

        conn.execute(
            f"""
            UPDATE projects
            SET
                elbow_status = NULL,
                elbow_error = NULL,
                elbow_commit = NULL,
                elbow_commit_date = NULL
            WHERE project_uid IN ({placeholders})
            """,
            project_uids,
        )

        conn.commit()

    finally:
        conn.close()


# ----------------------------
# Pipeline runs
# ----------------------------
def run_core_pipeline() -> None:
    """Run the main metric extraction pipeline without elbow metrics."""
    df_projects = load_projects_from_db()

    if df_projects.empty:
        print("No projects found in the database.")
        return

    print(f"Projects selected for core pipeline: {len(df_projects)}")

    run_pipeline(
        df_projects,
        force=False,
        run_name="core_pipeline",
        resume_progress=True,
        print_metric_changes=True,
        print_unchanged=False,
        extract_elbow_metrics=False,
    )


def rerun_elbow_github_error_projects() -> None:
    """
    Rerun only elbow extraction for projects that previously failed due to
    GitHub DNS/connection errors.
    """
    df_retry = load_elbow_github_error_projects()

    if df_retry.empty:
        print("No elbow GitHub DNS/error rows found to retry.")
        return

    print(f"Elbow GitHub error rows selected for retry: {len(df_retry)}")

    # Reset only failed elbow fields so the pipeline selects these rows cleanly.
    clear_previous_elbow_error_flags(df_retry)

    run_pipeline(
        df_retry,
        force=False,
        run_name="rerun_elbow_github_dns_errors_v1",
        resume_progress=False,
        print_metric_changes=True,
        print_unchanged=False,
        extract_elbow_metrics=True,
        elbow_days=11,
    )


def main() -> None:
    """Run the default pipeline."""
    run_core_pipeline()

    # To include the elbow-based continuation extraction instead, either:
    # 1. set extract_elbow_metrics=True in run_core_pipeline(), or
    # 2. call rerun_elbow_github_error_projects() for the retry-only case.


if __name__ == "__main__":
    main()