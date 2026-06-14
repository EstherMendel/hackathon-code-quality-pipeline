# pipeline.py
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
from tqdm import tqdm

import metrics_js
import metrics_python
from cleanup_utils import robust_rmtree
from config import COMMIT_EVERY, DB_PATH, MAX_WORKERS, REPOS_DIR
from db import (
    ensure_db_schema,
    ensure_files_db_schema,
    load_projects_table,
    needs_elbow_update_from_db_row,
    needs_update_from_db_row,
    open_files_db_connection,
    write_file_metrics_conn,
    write_row_conn,
)
from git_utils import (
    checkout_commit_before_date,
    checkout_last_commit_within_elbow,
    clone_or_reuse_repo,
    get_commit_date,
    repo_name_from_url,
)
from logging_utils import log
from metrics_common import (
    count_detectable_source_files,
    is_excluded_dir,
    iter_source_files,
)
from normalize import normalize_link


# ----------------------------
# Progress files
# ----------------------------
RERUN_PROGRESS_DIR = Path(DB_PATH).resolve().parent / "rerun_progress"
RERUN_PROGRESS_DIR.mkdir(parents=True, exist_ok=True)


def _progress_file_path(run_name: str | None = None) -> Path:
    """Return the progress file path for a pipeline run."""
    run_name = (run_name or "full_rerun").strip()
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in run_name)
    return RERUN_PROGRESS_DIR / f"{safe}_completed_uids.txt"


def _load_completed_uids(progress_file: Path) -> set[str]:
    """Load project IDs already completed in a previous interrupted run."""
    if not progress_file.exists():
        return set()

    completed = set()

    try:
        for line in progress_file.read_text(encoding="utf8", errors="ignore").splitlines():
            project_uid = line.strip()
            if project_uid:
                completed.add(project_uid)

    except Exception as e:
        log(f"[PROGRESS] WARNING: could not read progress file {progress_file}: {e}")

    return completed


def _append_completed_uid(progress_file: Path, project_uid: str) -> None:
    """Mark one project as completed for the current run."""
    try:
        with open(progress_file, "a", encoding="utf8") as fh:
            fh.write(f"{project_uid}\n")

    except Exception as e:
        log(f"[PROGRESS] WARNING: could not append {project_uid} to {progress_file}: {e}")


# ----------------------------
# Row and repository helpers
# ----------------------------
def _analysis_date_from_row(row) -> str:
    """Get the hackathon end date used for the repository snapshot."""
    for col in ["end_date", "End_Date", "End_date", "date", "hackathon_date"]:
        if col not in row.index:
            continue

        date_value = row.get(col)
        if date_value is None:
            continue

        try:
            ts = pd.to_datetime(date_value, errors="coerce")
            if pd.notna(ts):
                return ts.strftime("%Y-%m-%d")

        except Exception:
            pass

    return datetime.utcnow().strftime("%Y-%m-%d")


def _iter_code_files_all(repo_path: str) -> int:
    """
    Count all code-like files after exclusions.

    This is broader than the Python/JavaScript metric extraction and is used
    to estimate how much of the repository is covered by the selected language.
    """
    code_exts = (
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".scala",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".go",
        ".rb",
        ".php",
        ".rs",
        ".swift",
        ".r",
        ".m",
        ".jl",
        ".lua",
        ".sh",
    )

    total = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not is_excluded_dir(d, exclude_generated=True)]

        for file_name in files:
            if file_name.lower().endswith(code_exts):
                total += 1

    return total


def _count_lang_files(repo_path: str, lang: str) -> int:
    """Count analyzable files for the selected language."""
    if lang not in ("python", "javascript"):
        return 0

    return sum(1 for _ in iter_source_files(repo_path, lang, exclude_generated=True))


def _detect_lang(repo_path: str) -> str | None:
    """Detect whether Python or JavaScript/TypeScript is dominant."""
    counts = count_detectable_source_files(repo_path, exclude_generated=True)

    py = counts["python_files"]
    js = counts["javascript_files"]

    if py == 0 and js == 0:
        return None

    return "python" if py >= js else "javascript"


# ----------------------------
# Metric comparison helpers
# ----------------------------
def _to_float_or_none(value):
    """Convert numeric values for comparison."""
    try:
        if value is None:
            return None
        if pd.isna(value):
            return None
        return float(value)

    except Exception:
        return None


def _metric_changed(old_value, new_value, tol: float = 1e-12) -> bool:
    """Check whether a stored metric changed after rerunning extraction."""
    old_float = _to_float_or_none(old_value)
    new_float = _to_float_or_none(new_value)

    if old_float is None and new_float is None:
        return False

    if old_float is None or new_float is None:
        return True

    return abs(old_float - new_float) > tol


def _collect_changed_metrics(old_row, new_row: dict) -> list[tuple[str, object, object]]:
    """Collect changed project-level metrics for rerun logging."""
    metric_keys = [
        "lang",
        "status",
        "error",
        "lang_files_count",
        "all_source_files_count",
        "lang_file_ratio_all",
        "files_count",
        "readme_words",
        "lloc_total",
        "sloc_total",
        "functions_total",
        "cc_total",
        "comment_words_total",
        "fan_in_mean",
        "fan_in_median",
        "fan_in_max",
        "fan_in_p95",
        "fan_out_mean",
        "fan_out_median",
        "fan_out_max",
        "fan_out_p95",
        "external_deps_static_count",
        "external_deps_declared_count",
        "external_deps_union_count",
        "parse_ok_files",
        "parse_fail_files",
        "parse_fail_ratio",
        "suspect_all_zero",
    ]

    changes = []

    for key in metric_keys:
        old_value = old_row.get(key) if old_row is not None else None
        new_value = new_row.get(key)

        if key in {"lang", "status", "error"}:
            old_string = None if old_value is None or pd.isna(old_value) else str(old_value)
            new_string = None if new_value is None else str(new_value)

            if old_string != new_string:
                changes.append((key, old_value, new_value))

            continue

        if _metric_changed(old_value, new_value):
            changes.append((key, old_value, new_value))

    return changes


def _print_repo_metric_changes(
    project_uid: str,
    repo_name: str,
    changes: list[tuple[str, object, object]],
    max_lines: int = 12,
    print_unchanged: bool = False,
) -> None:
    """Print a compact overview of changed metrics for one repository."""
    if not changes:
        if print_unchanged:
            print(f"[UNCHANGED] {project_uid} ({repo_name})")
        return

    print(f"\n[CHANGED] {project_uid} ({repo_name})")

    for key, old_value, new_value in changes[:max_lines]:
        print(f"  - {key}: {old_value} -> {new_value}")

    if len(changes) > max_lines:
        print(f"  ... and {len(changes) - max_lines} more change(s)")


# ----------------------------
# Metric extraction
# ----------------------------
def _extract_metrics_from_current_checkout(repo_path: str) -> tuple[str | None, dict, list, dict]:
    """
    Extract metrics from the currently checked out commit.

    Returns the detected language, project-level metrics, per-file records,
    and a small metadata dictionary with status/error information.
    """
    lang = _detect_lang(repo_path)

    meta = {
        "lang": lang,
        "status": "ok",
        "error": "",
        "lang_files_count": None,
        "all_source_files_count": None,
        "lang_file_ratio_all": None,
    }

    if lang is None:
        meta["status"] = "error-permanent"
        meta["error"] = "Could not detect python/javascript from repository files"
        return None, {}, [], meta

    all_code = _iter_code_files_all(repo_path)
    lang_files = _count_lang_files(repo_path, lang)

    meta["lang_files_count"] = float(lang_files)
    meta["all_source_files_count"] = float(all_code)
    meta["lang_file_ratio_all"] = (lang_files / all_code) if all_code else None

    if lang_files == 0:
        meta["status"] = "error-permanent"
        meta["error"] = "No analyzable python/javascript source files after exclusions"
        return lang, {}, [], meta

    if lang == "python":
        metrics, per_file = metrics_python.extract_metrics(repo_path, exclude_generated=True)
    else:
        metrics, per_file = metrics_js.extract_metrics(repo_path, exclude_generated=True)

    files_count = int(metrics.get("files_count") or 0)
    sloc_total = int(metrics.get("sloc_total") or 0)
    lloc_total = int(metrics.get("lloc_total") or 0)
    functions_total = int(metrics.get("functions_total") or 0)
    suspect_all_zero = int(metrics.get("suspect_all_zero") or 0)

    parse_ok_files = int(metrics.get("parse_ok_files") or 0)
    parse_fail_files = int(metrics.get("parse_fail_files") or 0)

    if files_count == 0:
        meta["status"] = "error-permanent"
        meta["error"] = "No analyzable python/javascript source files after exclusions"
        return lang, metrics, per_file, meta

    if suspect_all_zero == 1 or (
        sloc_total == 0
        and lloc_total == 0
        and functions_total == 0
    ):
        meta["status"] = "error"
        meta["error"] = "Suspect all-zero metrics (parse failures or files excluded/empty)"
        return lang, metrics, per_file, meta

    parse_total = parse_ok_files + parse_fail_files

    if parse_total > 0 and parse_ok_files == 0:
        meta["status"] = "error"
        meta["error"] = f"All files failed to parse ({parse_fail_files}/{parse_total})"
        return lang, metrics, per_file, meta

    return lang, metrics, per_file, meta


# ----------------------------
# Repository workers
# ----------------------------
def _process_one_repo(task: tuple) -> dict:
    """
    Process one repository for the core snapshot.

    If elbow extraction is enabled, the same worker also extracts metrics for
    the latest commit within the elbow window.
    """
    if len(task) == 4:
        idx, link_norm, project_uid, row = task
        extract_elbow_metrics = False
        elbow_days = 11
    else:
        idx, link_norm, project_uid, row, extract_elbow_metrics, elbow_days = task

    repo_name = repo_name_from_url(link_norm)
    analysis_date = _analysis_date_from_row(row)

    result = {
        "project_uid": project_uid,
        "github_link": link_norm,
        "repo_name": repo_name,
        "analysis_date": analysis_date,
        "lang": None,
        "commit": None,
        "commit_date": None,
        "status": "ok",
        "error": "",
        "_per_file_records": [],
        "_elbow_per_file_records": [],
    }

    if extract_elbow_metrics:
        result.update(
            {
                "elbow_commit": None,
                "elbow_commit_date": None,
                "elbow_days": float(elbow_days),
                "elbow_status": None,
                "elbow_error": "",
            }
        )

    ok, repo_path, err = clone_or_reuse_repo(link_norm, str(REPOS_DIR))

    if not ok:
        result["status"] = "error"
        result["error"] = f"clone failed: {err}"

        if extract_elbow_metrics:
            result["elbow_status"] = "not_run"
            result["elbow_error"] = "core clone failed"

        return result

    try:
        # Core snapshot: last commit before the hackathon end date.
        commit, diag = checkout_commit_before_date(repo_path, analysis_date)

        if not commit:
            result["status"] = "no_valid_commit"
            result["error"] = diag or f"No commit exists before {analysis_date}"

            if extract_elbow_metrics:
                result["elbow_status"] = "not_run"
                result["elbow_error"] = "no core commit before analysis_date"

            return result

        result["commit"] = commit
        result["commit_date"] = get_commit_date(repo_path, commit)

        lang, metrics, per_file, meta = _extract_metrics_from_current_checkout(repo_path)

        result["lang"] = lang
        result["status"] = meta["status"]
        result["error"] = meta["error"]
        result["lang_files_count"] = meta["lang_files_count"]
        result["all_source_files_count"] = meta["all_source_files_count"]
        result["lang_file_ratio_all"] = meta["lang_file_ratio_all"]

        result.update(metrics)
        result["_per_file_records"] = per_file

        # Store diagnostics and stop if the core snapshot is not valid.
        if result["status"] != "ok":
            if extract_elbow_metrics:
                result["elbow_status"] = "not_run"
                result["elbow_error"] = f"core status was {result['status']}"

            return result

        # Optional continuation snapshot within the elbow window.
        if extract_elbow_metrics:
            elbow_commit, elbow_diag = checkout_last_commit_within_elbow(
                repo_path=repo_path,
                start_date=analysis_date,
                elbow_days=elbow_days,
            )

            if not elbow_commit:
                result["elbow_status"] = "no_elbow_commit"
                result["elbow_error"] = elbow_diag or (
                    f"No commit after {analysis_date} and within {elbow_days} days"
                )
                return result

            result["elbow_commit"] = elbow_commit
            result["elbow_commit_date"] = get_commit_date(repo_path, elbow_commit)

            elbow_lang, elbow_metrics, elbow_per_file, elbow_meta = (
                _extract_metrics_from_current_checkout(repo_path)
            )

            result["elbow_status"] = elbow_meta["status"]
            result["elbow_error"] = elbow_meta["error"]
            result["elbow_lang"] = elbow_lang
            result["elbow_lang_files_count"] = elbow_meta["lang_files_count"]
            result["elbow_all_source_files_count"] = elbow_meta["all_source_files_count"]
            result["elbow_lang_file_ratio_all"] = elbow_meta["lang_file_ratio_all"]

            for key, value in elbow_metrics.items():
                result[f"elbow_{key}"] = value

            result["_elbow_per_file_records"] = elbow_per_file

        return result

    except Exception as e:
        result["status"] = "error"
        result["error"] = f"processing failed: {e}"

        if extract_elbow_metrics and not result.get("elbow_status"):
            result["elbow_status"] = "error"
            result["elbow_error"] = f"processing failed before/during elbow extraction: {e}"

        return result

    finally:
        ok = robust_rmtree(repo_path, retries=8, delay=0.4)

        if not ok:
            log(f"[CLEANUP] WARNING: Could not fully delete repo folder: {repo_path}")
        else:
            log(f"[CLEANUP] Deleted repo folder: {repo_path}")


def _process_one_repo_elbow_only(task: tuple) -> dict:
    """
    Process one repository for elbow metrics only.

    This keeps the original/core metrics unchanged and only fills elbow_*
    columns plus optional per-file records with variant='elbow'.
    """
    idx, link_norm, project_uid, row, elbow_days = task

    repo_name = repo_name_from_url(link_norm)
    analysis_date = _analysis_date_from_row(row)

    result = {
        "project_uid": project_uid,
        "github_link": link_norm,
        "repo_name": repo_name,
        "analysis_date": analysis_date,
        "elbow_commit": None,
        "elbow_commit_date": None,
        "elbow_days": float(elbow_days),
        "elbow_status": "ok",
        "elbow_error": "",
        "_elbow_per_file_records": [],
    }

    ok, repo_path, err = clone_or_reuse_repo(link_norm, str(REPOS_DIR))

    if not ok:
        result["elbow_status"] = "error"
        result["elbow_error"] = f"clone failed: {err}"
        return result

    try:
        elbow_commit, elbow_diag = checkout_last_commit_within_elbow(
            repo_path=repo_path,
            start_date=analysis_date,
            elbow_days=elbow_days,
        )

        if not elbow_commit:
            result["elbow_status"] = "no_elbow_commit"
            result["elbow_error"] = elbow_diag or (
                f"No commit after {analysis_date} and within {elbow_days} days"
            )
            return result

        result["elbow_commit"] = elbow_commit
        result["elbow_commit_date"] = get_commit_date(repo_path, elbow_commit)

        lang, metrics, per_file, meta = _extract_metrics_from_current_checkout(repo_path)

        result["elbow_status"] = meta["status"]
        result["elbow_error"] = meta["error"]
        result["elbow_lang_files_count"] = meta["lang_files_count"]
        result["elbow_all_source_files_count"] = meta["all_source_files_count"]
        result["elbow_lang_file_ratio_all"] = meta["lang_file_ratio_all"]

        for key, value in metrics.items():
            result[f"elbow_{key}"] = value

        result["_elbow_per_file_records"] = per_file

        return result

    except Exception as e:
        result["elbow_status"] = "error"
        result["elbow_error"] = f"elbow processing failed: {e}"
        return result

    finally:
        ok = robust_rmtree(repo_path, retries=8, delay=0.4)

        if not ok:
            log(f"[CLEANUP] WARNING: Could not fully delete repo folder: {repo_path}")
        else:
            log(f"[CLEANUP] Deleted repo folder: {repo_path}")


# ----------------------------
# Pipeline setup helpers
# ----------------------------
def _clean_project_columns(df_projects: pd.DataFrame) -> pd.DataFrame:
    """Clean basic project identifiers before creating the todo list."""
    df_projects = df_projects.copy()

    for col in ["project_uid", "github_link", "repo_name"]:
        if col in df_projects.columns:
            df_projects[col] = df_projects[col].where(df_projects[col].notna(), None)
            df_projects[col] = df_projects[col].map(
                lambda x: x.strip() if isinstance(x, str) else x
            )

    df_projects["github_link_norm"] = df_projects["github_link"].apply(normalize_link)
    return df_projects.reset_index(drop=True)


def _make_db_index(df_db: pd.DataFrame):
    """Index current database rows by project_uid."""
    if df_db.empty or "project_uid" not in df_db.columns:
        return None

    df_db_idx = (
        df_db
        .dropna(subset=["project_uid"])
        .drop_duplicates(subset=["project_uid"], keep="last")
    )

    return df_db_idx.set_index("project_uid", drop=False)


def _make_db_rows_by_uid(db_index) -> dict:
    """Convert the database index to plain dictionaries for quick lookup."""
    if db_index is None:
        return {}

    return {
        str(uid).strip(): db_index.loc[uid].to_dict()
        for uid in db_index.index
    }


def _make_todo_list(
    df_projects: pd.DataFrame,
    db_index,
    completed_uids: set[str],
    resume_progress: bool,
    force: bool,
    extract_elbow_metrics: bool,
    elbow_days: int,
) -> tuple[list[tuple], int]:
    """Build the list of repositories that need to be processed."""
    todo = []
    skipped_by_progress = 0

    for local_idx, row in df_projects.iterrows():
        project_uid = row.project_uid if "project_uid" in row.index else None
        link_norm = row.github_link_norm if "github_link_norm" in row.index else None

        if not project_uid or str(project_uid).strip().lower() in {"nan", "none", ""}:
            continue

        if not link_norm or str(link_norm).strip().lower() in {"nan", "none", ""}:
            continue

        project_uid = str(project_uid).strip()
        link_norm = str(link_norm).strip()

        # This makes interrupted reruns resumable even when force=True.
        if resume_progress and project_uid in completed_uids:
            skipped_by_progress += 1
            continue

        db_row = None
        if db_index is not None and project_uid in db_index.index:
            db_row = db_index.loc[project_uid]

        if extract_elbow_metrics:
            needs_rerun = force or needs_elbow_update_from_db_row(db_row)
        else:
            needs_rerun = force or needs_update_from_db_row(db_row)

        if not needs_rerun:
            continue

        if extract_elbow_metrics:
            todo.append((local_idx, link_norm, project_uid, row, elbow_days))
        else:
            todo.append((local_idx, link_norm, project_uid, row))

    return todo, skipped_by_progress


# ----------------------------
# Result writing
# ----------------------------
def _print_processing_result(
    project_row: dict,
    old_row: dict | None,
    extract_elbow_metrics: bool,
    print_unchanged: bool,
) -> None:
    """Print status or metric changes after processing one repository."""
    project_uid = str(project_row.get("project_uid") or "").strip()
    repo_name = str(project_row.get("repo_name") or "")

    if extract_elbow_metrics:
        elbow_status = str(project_row.get("elbow_status") or "")

        if elbow_status not in {"ok", "no_elbow_commit", "not_run"}:
            print(
                f"\n[ELBOW ERROR] {project_uid} ({repo_name}) "
                f"elbow_status={project_row.get('elbow_status')} "
                f"elbow_error={project_row.get('elbow_error') or '<empty>'}"
            )

        elif print_unchanged:
            print(
                f"[ELBOW] {project_uid} ({repo_name}) "
                f"elbow_status={project_row.get('elbow_status')}"
            )

        return

    new_status = str(project_row.get("status") or "")

    if new_status != "ok":
        print(
            f"\n[RERUN ERROR] {project_uid} ({repo_name}) "
            f"status={project_row.get('status')} "
            f"error={project_row.get('error') or '<empty>'}"
        )
        return

    changes = _collect_changed_metrics(old_row, project_row)

    _print_repo_metric_changes(
        project_uid=project_uid,
        repo_name=repo_name,
        changes=changes,
        print_unchanged=print_unchanged,
    )


def _write_per_file_metrics(
    files_conn,
    project_row: dict,
    old_row: dict | None,
    per_file: list,
    elbow_per_file: list,
    extract_elbow_metrics: bool,
) -> None:
    """Write core or elbow per-file metrics to the files database."""
    if extract_elbow_metrics:
        if not (
            project_row.get("project_uid")
            and project_row.get("elbow_status") == "ok"
            and elbow_per_file
        ):
            return

        lang_for_files = (
            project_row.get("elbow_lang")
            or project_row.get("lang")
            or (old_row.get("lang") if old_row else None)
        )

        if lang_for_files:
            write_file_metrics_conn(
                files_conn,
                project_row["project_uid"],
                "elbow",
                lang_for_files,
                elbow_per_file,
            )

        return

    if project_row.get("project_uid") and project_row.get("lang"):
        write_file_metrics_conn(
            files_conn,
            project_row["project_uid"],
            "core",
            project_row["lang"],
            per_file,
        )


def _handle_finished_result(
    res: dict,
    conn,
    files_conn,
    db_rows_by_uid: dict,
    progress_file: Path,
    completed_uids: set[str],
    print_metric_changes: bool,
    print_unchanged: bool,
    extract_elbow_metrics: bool,
) -> None:
    """Write one worker result and update progress."""
    project_row = dict(res)
    per_file = project_row.pop("_per_file_records", []) or []
    elbow_per_file = project_row.pop("_elbow_per_file_records", []) or []

    project_uid = str(project_row.get("project_uid") or "").strip()
    old_row = db_rows_by_uid.get(project_uid)

    if print_metric_changes:
        _print_processing_result(
            project_row=project_row,
            old_row=old_row,
            extract_elbow_metrics=extract_elbow_metrics,
            print_unchanged=print_unchanged,
        )

    write_row_conn(conn, project_row)

    _write_per_file_metrics(
        files_conn=files_conn,
        project_row=project_row,
        old_row=old_row,
        per_file=per_file,
        elbow_per_file=elbow_per_file,
        extract_elbow_metrics=extract_elbow_metrics,
    )

    db_rows_by_uid[project_uid] = dict(project_row)

    # Only mark completed after database writes succeeded.
    if project_uid:
        _append_completed_uid(progress_file, project_uid)
        completed_uids.add(project_uid)


# ----------------------------
# Main pipeline
# ----------------------------
def run_pipeline(
    df_projects,
    force: bool = False,
    run_name: str | None = None,
    resume_progress: bool = True,
    print_metric_changes: bool = True,
    print_unchanged: bool = False,
    extract_elbow_metrics: bool = False,
    elbow_days: int = 11,
):
    """
    Run metric extraction for a set of project repositories.

    Parameters
    ----------
    df_projects:
        Project rows with at least project_uid and github_link.
    force:
        If True, rerun even when metrics already exist.
    run_name:
        Name used for the progress file.
    resume_progress:
        If True, skip project_uids already completed in this run.
    print_metric_changes:
        If True, print differences compared with the current DB row.
    print_unchanged:
        If True, also print repositories without changed metrics.
    extract_elbow_metrics:
        If True, extract only elbow metrics for eligible rows.
    elbow_days:
        Number of days after the analysis date used for the elbow window.
    """
    log(f"Total projects in input: {len(df_projects)}")

    ensure_db_schema()
    ensure_files_db_schema()

    df_projects = _clean_project_columns(df_projects)

    progress_file = _progress_file_path(run_name)
    completed_uids = _load_completed_uids(progress_file) if resume_progress else set()

    if resume_progress:
        log(f"[PROGRESS] Using progress file: {progress_file}")
        log(f"[PROGRESS] Already completed in this rerun: {len(completed_uids)}")

    conn = sqlite3.connect(str(DB_PATH))
    files_conn = open_files_db_connection()

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        df_db = load_projects_table(conn)
        db_index = _make_db_index(df_db)
        db_rows_by_uid = _make_db_rows_by_uid(db_index)

        todo, skipped_by_progress = _make_todo_list(
            df_projects=df_projects,
            db_index=db_index,
            completed_uids=completed_uids,
            resume_progress=resume_progress,
            force=force,
            extract_elbow_metrics=extract_elbow_metrics,
            elbow_days=elbow_days,
        )

        if resume_progress and skipped_by_progress:
            log(f"[PROGRESS] Skipped already-completed repos from this rerun: {skipped_by_progress}")

        if not todo:
            log("Nothing to do — all projects up to date or already completed in this rerun.")
            return df_db

        log(f"\nProcessing {len(todo)} repos...\n")

        conn.execute("BEGIN")
        files_conn.execute("BEGIN")

        processed = 0
        worker_fn = _process_one_repo_elbow_only if extract_elbow_metrics else _process_one_repo

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(worker_fn, task) for task in todo]

            for fut in tqdm(as_completed(futures), total=len(futures)):
                try:
                    res = fut.result()

                    _handle_finished_result(
                        res=res,
                        conn=conn,
                        files_conn=files_conn,
                        db_rows_by_uid=db_rows_by_uid,
                        progress_file=progress_file,
                        completed_uids=completed_uids,
                        print_metric_changes=print_metric_changes,
                        print_unchanged=print_unchanged,
                        extract_elbow_metrics=extract_elbow_metrics,
                    )

                    processed += 1

                    if processed % COMMIT_EVERY == 0:
                        conn.commit()
                        files_conn.commit()
                        conn.execute("BEGIN")
                        files_conn.execute("BEGIN")
                        log(f"[DB] committed at {processed}")

                except Exception as e:
                    log(f"[PIPELINE][ERROR] write/result failed: {e}")
                    conn.rollback()
                    files_conn.rollback()
                    conn.execute("BEGIN")
                    files_conn.execute("BEGIN")

        conn.commit()
        files_conn.commit()

        df_final = load_projects_table(conn)

        log("\nDONE — all results saved.")
        log(f"[PROGRESS] Completed rerun progress file: {progress_file}")

        return df_final

    finally:
        try:
            files_conn.close()
        except Exception:
            pass

        conn.close()