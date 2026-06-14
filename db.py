# db.py
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List
import pandas as pd

from config import DB_PATH, FILES_DB_PATH


# ----------------------------
# Metric column definitions
# ----------------------------
METRIC_KEYS_CORE = [
    "files_count",
    "readme_words",

    "lloc_total",
    "sloc_total",

    "sloc_mean_per_file",
    "sloc_median_per_file",
    "sloc_min_per_file",
    "sloc_max_per_file",

    "functions_total",

    "function_length_sloc_mean",
    "function_length_sloc_median",
    "function_length_sloc_min",
    "function_length_sloc_max",

    "function_density_per_sloc",
    "function_density_per_lloc",

    "cc_total",
    "cc_mean_per_function",
    "cc_median_per_function",
    "cc_min_per_function",
    "cc_max_per_function",

    "cc_density_per_sloc",
    "cc_density_per_lloc",

    "comment_words_total",
    "comment_words_density_per_sloc",
    "comment_words_density_per_lloc",

    "fan_in_mean",
    "fan_in_median",
    "fan_in_min",
    "fan_in_max",
    "fan_in_p95",

    "fan_out_mean",
    "fan_out_median",
    "fan_out_min",
    "fan_out_max",
    "fan_out_p95",

    "fan_in_list_json",
    "fan_out_list_json",

    "external_deps_static_count",
    "external_deps_declared_count",
    "external_deps_union_count",
    "external_deps_union_density_per_sloc",
    "external_deps_union_density_per_lloc",

    "parse_ok_files",
    "parse_fail_files",
    "parse_fail_ratio",
    "suspect_all_zero",

    "lang_files_count",
    "all_source_files_count",
    "lang_file_ratio_all",
]

ELBOW_PREFIX = "elbow_"

METRIC_KEYS_ELBOW = [
    f"{ELBOW_PREFIX}{key}"
    for key in METRIC_KEYS_CORE
]

ELBOW_BASE_COLS = [
    ("elbow_lang", "TEXT"),
    ("elbow_commit", "TEXT"),
    ("elbow_commit_date", "TEXT"),
    ("elbow_days", "REAL"),
    ("elbow_status", "TEXT"),
    ("elbow_error", "TEXT"),
]

# Used by needs_update_from_db_row().
METRIC_KEYS_ALL = METRIC_KEYS_CORE


# ----------------------------
# Generic helpers
# ----------------------------
def _sqlite_safe(value):
    """Convert values to SQLite-safe scalar or JSON/string values."""
    if value is None:
        return None

    if isinstance(value, (int, float, str, bytes)):
        return value

    try:
        import numpy as np

        if isinstance(value, (np.integer, np.floating)):
            return value.item()

    except Exception:
        pass

    try:
        return json.dumps(value, ensure_ascii=False, default=str)

    except Exception:
        return str(value)


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Return column names for a SQLite table."""
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table}")')
    return [row[1] for row in cur.fetchall()]


def _coerce_db_row_to_series(db_row):
    """Convert a possible database row object to a pandas Series."""
    if db_row is None:
        return None

    if isinstance(db_row, pd.DataFrame):
        if db_row.empty:
            return None

        return db_row.iloc[-1]

    if isinstance(db_row, pd.Series):
        return db_row

    return None


def _metric_columns_with_types(metric_keys: list[str]) -> list[tuple[str, str]]:
    """Build database columns for metric keys."""
    return [
        (key, "TEXT" if key.endswith("_json") else "REAL")
        for key in metric_keys
    ]


# ----------------------------
# Projects table migration
# ----------------------------
def deduplicate_projects_keep_latest() -> None:
    """Keep only the latest row per project_uid."""
    conn = sqlite3.connect(str(DB_PATH))

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name='projects'
        """)

        if cur.fetchone() is None:
            return

        cur.execute("""
            DELETE FROM projects
            WHERE rowid NOT IN (
                SELECT MAX(rowid)
                FROM projects
                GROUP BY project_uid
            )
        """)

        conn.commit()

    finally:
        conn.close()


def migrate_projects_make_uid_unique_full() -> None:
    """
    Rebuild projects so project_uid is the primary key.

    Existing columns are preserved, and duplicate project_uid rows are reduced
    to the latest row by rowid.
    """
    conn = sqlite3.connect(str(DB_PATH))

    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name='projects'
        """)

        if cur.fetchone() is None:
            return

        info = cur.execute('PRAGMA table_info("projects")').fetchall()

        if any(name == "project_uid" and pk == 1 for (_cid, name, _typ, _notnull, _dflt, pk) in info):
            return

        cur.execute("""
            DELETE FROM projects
            WHERE project_uid IS NULL
               OR TRIM(project_uid) = ''
        """)

        cur.execute("""
            DELETE FROM projects
            WHERE rowid NOT IN (
                SELECT MAX(rowid)
                FROM projects
                GROUP BY project_uid
            )
        """)

        conn.commit()

        cur.execute('ALTER TABLE projects RENAME TO projects_old;')

        old_info = cur.execute('PRAGMA table_info("projects_old")').fetchall()

        cur.execute("""
            CREATE TABLE projects (
                project_uid TEXT PRIMARY KEY
            );
        """)

        for (_cid, name, col_type, _notnull, _dflt, _pk) in old_info:
            if name == "project_uid":
                continue

            cur.execute(f'ALTER TABLE projects ADD COLUMN "{name}" {col_type or "TEXT"}')

        old_cols = [row[1] for row in old_info]
        new_cols = [row[1] for row in cur.execute('PRAGMA table_info("projects")').fetchall()]
        common_cols = [col for col in old_cols if col in new_cols]

        col_list = ", ".join([f'"{col}"' for col in common_cols])

        cur.execute(f"""
            INSERT INTO projects ({col_list})
            SELECT {col_list}
            FROM projects_old
            WHERE project_uid IS NOT NULL
              AND TRIM(project_uid) <> ''
              AND rowid IN (
                  SELECT MAX(rowid)
                  FROM projects_old
                  WHERE project_uid IS NOT NULL
                    AND TRIM(project_uid) <> ''
                  GROUP BY project_uid
              );
        """)

        old_count = cur.execute("SELECT COUNT(*) FROM projects_old").fetchone()[0]
        new_count = cur.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

        if old_count > 0 and new_count == 0:
            raise RuntimeError("Migration copied 0 rows; leaving projects_old intact.")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_projects_uid
            ON projects(project_uid);
        """)

        cur.execute("DROP TABLE projects_old;")

        conn.commit()

    finally:
        conn.close()


# ----------------------------
# Projects table schema
# ----------------------------
def ensure_db_schema() -> None:
    """Create or update the projects table schema."""
    conn = sqlite3.connect(str(DB_PATH))

    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                project_uid TEXT
            );
        """)

        base_cols = [
            ("project_uid", "TEXT"),
            ("github_link", "TEXT"),
            ("repo_name", "TEXT"),
            ("lang", "TEXT"),
            ("analysis_date", "TEXT"),
            ("commit", "TEXT"),
            ("commit_date", "TEXT"),
            ("status", "TEXT"),
            ("error", "TEXT"),
        ]

        metric_cols = _metric_columns_with_types(METRIC_KEYS_CORE)
        elbow_metric_cols = _metric_columns_with_types(METRIC_KEYS_ELBOW)

        required_columns = (
            base_cols
            + metric_cols
            + ELBOW_BASE_COLS
            + elbow_metric_cols
        )

        for col, col_type in required_columns:
            try:
                cur.execute(f'ALTER TABLE projects ADD COLUMN "{col}" {col_type}')

            except sqlite3.OperationalError:
                pass

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_projects_uid
            ON projects(project_uid);
        """)

        conn.commit()

    finally:
        conn.close()


def load_projects_table(conn: sqlite3.Connection) -> pd.DataFrame:
    """Load the full projects table."""
    return pd.read_sql("SELECT * FROM projects", conn)


# ----------------------------
# Update decisions
# ----------------------------
def needs_update_from_db_row(db_row) -> bool:
    """Decide whether core metrics should be rerun for one project row."""
    db_row = _coerce_db_row_to_series(db_row)

    if db_row is None:
        return True

    status = db_row.get("status")

    if pd.isna(status):
        status = None

    elif isinstance(status, str):
        status = status.strip()

    error = db_row.get("error")

    if pd.isna(error):
        error = ""

    else:
        error = str(error).strip()

    # Permanent failures are not retried automatically.
    if status in ("error-permanent", "no_valid_commit"):
        return False

    # Known transient or infrastructure-level failures.
    transient_error_patterns = [
        "Could not resolve host: github.com",
        "git timeout after",
        "checkout failed: git timeout",
        "smudge filter lfs failed",
        "git-lfs",
        "Object does not exist on the server",
        "fatal: cannot copy",
        "could not write config file",
        "[WinError 3]",
        "Het systeem kan het opgegeven pad niet vinden",
        "Repository already exists on computer",
        "Updating files",
        "processing failed:",
        "clone failed:",
        "checkout failed:",
    ]

    if error and any(pattern in error for pattern in transient_error_patterns):
        return True

    # Any other non-ok status should be retried.
    if status != "ok":
        return True

    lang = db_row.get("lang")

    if isinstance(lang, str):
        lang = lang.strip().lower()

    if lang not in ("python", "javascript"):
        return True

    for col in METRIC_KEYS_ALL:
        if col not in db_row.index or pd.isna(db_row.get(col)):
            return True

    return False


def needs_elbow_update_from_db_row(db_row) -> bool:
    """Decide whether elbow metrics should be rerun for one project row."""
    db_row = _coerce_db_row_to_series(db_row)

    if db_row is None:
        return True

    elbow_status = db_row.get("elbow_status")

    if pd.isna(elbow_status):
        elbow_status = None

    elif isinstance(elbow_status, str):
        elbow_status = elbow_status.strip()

    # Already handled.
    if elbow_status in ("ok", "no_elbow_commit", "not_run"):
        return False

    return True


# ----------------------------
# Projects table writing
# ----------------------------
def write_row_conn(conn: sqlite3.Connection, row: dict) -> None:
    """Insert or update one project row."""
    project_uid = row.get("project_uid")

    if not project_uid:
        return

    cur = conn.cursor()
    columns = [col[1] for col in cur.execute('PRAGMA table_info("projects")')]

    # Only write columns that exist in the current database schema.
    valid = {
        key: row[key]
        for key in row.keys()
        if key in columns
    }

    valid["project_uid"] = project_uid

    cols = list(valid.keys())
    values = [_sqlite_safe(valid[key]) for key in cols]

    col_list = ", ".join([f'"{name}"' for name in cols])
    q_marks = ", ".join(["?"] * len(cols))

    update_cols = [
        name
        for name in cols
        if name != "project_uid"
    ]

    if not update_cols:
        cur.execute(
            """
            INSERT INTO projects("project_uid")
            VALUES (?)
            ON CONFLICT(project_uid) DO NOTHING
            """,
            (project_uid,),
        )
        return

    set_clause = ", ".join(
        [f'"{name}"=excluded."{name}"' for name in update_cols]
    )

    sql = f"""
        INSERT INTO projects ({col_list})
        VALUES ({q_marks})
        ON CONFLICT(project_uid) DO UPDATE SET
            {set_clause}
    """

    cur.execute(sql, values)


# ----------------------------
# file_metrics schema and migration
# ----------------------------
def ensure_files_db_schema() -> None:
    """Create or update the file-level metrics database schema."""
    conn = sqlite3.connect(str(FILES_DB_PATH))

    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS file_metrics (
                project_uid TEXT NOT NULL,
                variant TEXT NOT NULL,
                lang TEXT NOT NULL,
                rel_path TEXT NOT NULL,

                sloc REAL,
                lloc REAL,
                functions REAL,
                cc_total REAL,
                cc_mean_per_function REAL,
                comment_words REAL,
                fan_in REAL,
                fan_out REAL,
                parse_ok REAL,
                parse_error TEXT,

                PRIMARY KEY (project_uid, variant, lang, rel_path)
            );
        """)

        cols = set(_table_columns(conn, "file_metrics"))

        # Migrate old schema name: metrics_type -> variant.
        if "metrics_type" in cols and "variant" not in cols:
            cur.execute("ALTER TABLE file_metrics RENAME TO file_metrics_old")

            cur.execute("""
                CREATE TABLE file_metrics (
                    project_uid TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    lang TEXT NOT NULL,
                    rel_path TEXT NOT NULL,

                    sloc REAL,
                    lloc REAL,
                    functions REAL,
                    cc_total REAL,
                    cc_mean_per_function REAL,
                    comment_words REAL,
                    fan_in REAL,
                    fan_out REAL,
                    parse_ok REAL,
                    parse_error TEXT,

                    PRIMARY KEY (project_uid, variant, lang, rel_path)
                );
            """)

            old_cols = set(_table_columns(conn, "file_metrics_old"))
            select_parse_error = (
                "parse_error"
                if "parse_error" in old_cols
                else "NULL AS parse_error"
            )

            cur.execute(f"""
                INSERT OR REPLACE INTO file_metrics (
                    project_uid,
                    variant,
                    lang,
                    rel_path,
                    sloc,
                    lloc,
                    functions,
                    cc_total,
                    cc_mean_per_function,
                    comment_words,
                    fan_in,
                    fan_out,
                    parse_ok,
                    parse_error
                )
                SELECT
                    project_uid,
                    metrics_type AS variant,
                    lang,
                    rel_path,
                    sloc,
                    lloc,
                    functions,
                    cc_total,
                    cc_mean_per_function,
                    comment_words,
                    fan_in,
                    fan_out,
                    parse_ok,
                    {select_parse_error}
                FROM file_metrics_old
            """)

            cur.execute("DROP TABLE file_metrics_old")

        # Older file_metrics tables may not have parse_error yet.
        cols = set(_table_columns(conn, "file_metrics"))

        if "parse_error" not in cols:
            try:
                cur.execute('ALTER TABLE file_metrics ADD COLUMN parse_error TEXT')

            except sqlite3.OperationalError:
                pass

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_file_metrics_uid
            ON file_metrics(project_uid);
        """)

        conn.commit()

    finally:
        conn.close()


def open_files_db_connection() -> sqlite3.Connection:
    """Open the file metrics database with batch-friendly settings."""
    conn = sqlite3.connect(str(FILES_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def write_file_metrics_conn(
    conn: sqlite3.Connection,
    project_uid: str,
    variant: str,
    lang: str,
    per_file_records: List[Dict[str, Any]],
) -> None:
    """Replace file-level metrics for one project/language/variant."""
    if not per_file_records:
        return

    cols = set(_table_columns(conn, "file_metrics"))
    type_col = "variant" if "variant" in cols else "metrics_type"

    conn.execute(
        f"""
        DELETE FROM file_metrics
        WHERE project_uid = ?
          AND {type_col} = ?
          AND lang = ?
        """,
        (project_uid, variant, lang),
    )

    rows = []

    for record in per_file_records:
        parse_ok_value = record.get("parse_ok")

        is_parse_fail = (
            parse_ok_value == 0
            or parse_ok_value is False
            or str(parse_ok_value).strip() == "0"
        )

        parse_error = record.get("parse_error")

        if is_parse_fail and not parse_error:
            parse_error = "[unknown_parse_failure]"

        rows.append(
            (
                project_uid,
                variant,
                lang,
                record.get("rel_path"),
                _sqlite_safe(record.get("sloc")),
                _sqlite_safe(record.get("lloc")),
                _sqlite_safe(record.get("functions")),
                _sqlite_safe(record.get("cc_total")),
                _sqlite_safe(record.get("cc_mean_per_function")),
                _sqlite_safe(record.get("comment_words")),
                _sqlite_safe(record.get("fan_in")),
                _sqlite_safe(record.get("fan_out")),
                _sqlite_safe(record.get("parse_ok")),
                _sqlite_safe(parse_error),
            )
        )

    conn.executemany(
        f"""
        INSERT OR REPLACE INTO file_metrics (
            project_uid,
            {type_col},
            lang,
            rel_path,
            sloc,
            lloc,
            functions,
            cc_total,
            cc_mean_per_function,
            comment_words,
            fan_in,
            fan_out,
            parse_ok,
            parse_error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )