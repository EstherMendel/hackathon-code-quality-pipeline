# git_utils.py
import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from shutil import which
from urllib.parse import urlparse

from config import REPO_TIMEOUT, REPOS_DIR
from logging_utils import log


# ----------------------------
# Git executable and subprocess helpers
# ----------------------------
def find_git() -> str:
    """Find the Git executable on PATH or in common Windows locations."""
    git_path = which("git")

    if git_path and os.path.exists(git_path):
        return git_path

    for candidate in [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe",
    ]:
        if os.path.exists(candidate):
            return candidate

    raise FileNotFoundError("Git not found on PATH or standard locations.")


GIT = find_git()


def _decode_subprocess_output(raw) -> str:
    """Decode subprocess output with a few common encodings."""
    if raw is None:
        return ""

    if isinstance(raw, str):
        return raw

    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except Exception:
            pass

    return raw.decode("utf-8", errors="replace")


def _git_env() -> dict:
    """Set non-interactive Git options for batch processing."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    env["GIT_ASKPASS"] = "echo"
    env["SSH_ASKPASS"] = "echo"
    return env


def run_git(cmd, cwd, timeout=REPO_TIMEOUT):
    """Run a Git command with batch-safe defaults."""
    try:
        out = subprocess.check_output(
            [GIT] + cmd,
            cwd=cwd,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=False,
            env=_git_env(),
        )

        return True, _decode_subprocess_output(out).strip()

    except subprocess.TimeoutExpired as e:
        return (
            False,
            f"git timeout after {timeout}s: {' '.join(cmd)} | "
            f"{_decode_subprocess_output(e.output).strip()}".strip(),
        )

    except subprocess.CalledProcessError as e:
        return False, f"git error: {_decode_subprocess_output(e.output).strip()}".strip()

    except Exception as e:
        return False, str(e)


# ----------------------------
# Repository path helpers
# ----------------------------
def repo_name_from_url(url: str) -> str:
    """Extract the repository name from a GitHub URL."""
    base = os.path.basename((url or "").rstrip("/"))
    return base[:-4] if base.endswith(".git") else base


def _sanitize_fs_piece(value: str, max_len: int = 16) -> str:
    """Create a short filesystem-safe name part."""
    value = (value or "").strip().lower()
    value = value.replace(".git", "")
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")

    if not value:
        value = "repo"

    return value[:max_len]


def _unique_repo_dir_from_url(url: str) -> str:
    """
    Create a stable folder name for a repository URL.

    A short hash is included so repositories with the same name do not collide.
    """
    url = (url or "").strip()
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]

    # Normalize scp-like GitHub URLs for parsing.
    parsed_url = url.replace("git@github.com:", "https://github.com/")

    repo = None

    try:
        parsed = urlparse(parsed_url)
        parts = [part for part in (parsed.path or "").strip("/").split("/") if part]

        if len(parts) >= 2:
            repo = parts[1]
            if repo.endswith(".git"):
                repo = repo[:-4]

    except Exception:
        pass

    if not repo:
        base = os.path.basename(url.rstrip("/"))
        repo = base[:-4] if base.endswith(".git") else base

    repo_hint = _sanitize_fs_piece(repo, max_len=16)
    return f"r_{repo_hint}_{url_hash}"


# ----------------------------
# Cleanup and cloning
# ----------------------------
def _remove_tree_strict(path: str, retries: int = 6, delay: float = 0.3) -> tuple[bool, str | None]:
    """Remove a directory and check that it is really gone."""
    if not os.path.exists(path):
        return True, None

    try:
        from cleanup_utils import robust_rmtree

        ok = robust_rmtree(path, retries=retries, delay=delay)

        if ok and not os.path.exists(path):
            return True, None

    except Exception as e:
        log(f"[CLEANUP] robust_rmtree failed for {path}: {e}")

    try:
        shutil.rmtree(path, ignore_errors=True)

    except Exception as e:
        log(f"[CLEANUP] shutil.rmtree failed for {path}: {e}")

    if os.path.exists(path):
        return False, f"existing repo dir could not be removed: {path}"

    return True, None


def clone_or_reuse_repo(link: str, base_dir: str | None = None):
    """
    Clone a repository into a temporary folder, validate it, then promote it.

    The folder name is based on the full URL, so parallel runs do not collide
    when different users have repositories with the same name.
    """
    if base_dir is None:
        base_dir = str(REPOS_DIR)

    Path(base_dir).mkdir(parents=True, exist_ok=True)

    repo_dir = _unique_repo_dir_from_url(link)
    repo_path = os.path.join(base_dir, repo_dir)
    tmp_repo_path = repo_path + "__tmp__"

    if os.path.isdir(repo_path):
        removed, err = _remove_tree_strict(repo_path, retries=6, delay=0.3)

        if not removed:
            return False, None, f"clone precondition failed: {err}"

    if os.path.isdir(tmp_repo_path):
        removed, err = _remove_tree_strict(tmp_repo_path, retries=6, delay=0.3)

        if not removed:
            return False, None, f"clone temp cleanup failed: {err}"

    ok, out = run_git(
        [
            "-c", "filter.lfs.smudge=",
            "-c", "filter.lfs.process=",
            "-c", "filter.lfs.required=false",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "clone",
            "--no-checkout",
            "--config", "core.longpaths=true",
            link,
            tmp_repo_path,
        ],
        cwd=base_dir,
        timeout=REPO_TIMEOUT,
    )

    if not ok:
        _remove_tree_strict(tmp_repo_path, retries=4, delay=0.2)
        return False, None, out

    # Validate clone before moving it to the final path.
    git_dir = os.path.join(tmp_repo_path, ".git")

    if not os.path.isdir(tmp_repo_path) or not os.path.exists(git_dir):
        _remove_tree_strict(tmp_repo_path, retries=4, delay=0.2)
        return False, None, f"clone failed: target is not a valid git repository: {tmp_repo_path}"

    ok, out = run_git(
        ["rev-parse", "--is-inside-work-tree"],
        cwd=tmp_repo_path,
        timeout=REPO_TIMEOUT,
    )

    if not ok:
        _remove_tree_strict(tmp_repo_path, retries=4, delay=0.2)
        return False, None, f"clone validation failed: {out}"

    ok, out = run_git(
        ["show-ref"],
        cwd=tmp_repo_path,
        timeout=REPO_TIMEOUT,
    )

    if not ok:
        _remove_tree_strict(tmp_repo_path, retries=4, delay=0.2)
        return False, None, f"clone validation failed: {out}"

    try:
        os.replace(tmp_repo_path, repo_path)

    except Exception as e:
        _remove_tree_strict(tmp_repo_path, retries=4, delay=0.2)
        return False, None, f"clone finalization failed: {e}"

    return True, repo_path, None


# ----------------------------
# Branch and history helpers
# ----------------------------
def get_default_branch(repo_path: str) -> str:
    """Find the remote default branch without checking out the worktree."""
    ok, out = run_git(
        ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    if ok and (out or "").strip():
        return (out or "").strip().rsplit("/", 1)[-1]

    for candidate in ["main", "master"]:
        ok, _ = run_git(
            ["rev-parse", "--verify", f"refs/remotes/origin/{candidate}"],
            cwd=repo_path,
            timeout=REPO_TIMEOUT,
        )

        if ok:
            return candidate

    ok, out = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    if ok and (out or "").strip():
        refs = [line.strip() for line in out.splitlines() if line.strip()]
        refs = [ref for ref in refs if ref != "origin/HEAD"]

        if refs:
            return refs[0].rsplit("/", 1)[-1]

    return "main"


def _candidate_history_refs(repo_path: str) -> list[str]:
    """Return remote refs to inspect for historical commits."""
    refs: list[str] = []

    branch = get_default_branch(repo_path)

    if branch:
        refs.extend(
            [
                f"refs/remotes/origin/{branch}",
                f"origin/{branch}",
            ]
        )

    for candidate in ("main", "master"):
        refs.extend(
            [
                f"refs/remotes/origin/{candidate}",
                f"origin/{candidate}",
            ]
        )

    ok, out = run_git(
        ["for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    if ok and (out or "").strip():
        for line in out.splitlines():
            ref = line.strip()

            if ref and ref not in refs and ref != "refs/remotes/origin/HEAD":
                refs.append(ref)

    seen = set()
    out_refs = []

    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            out_refs.append(ref)

    return out_refs


def _first_valid_ref(repo_path: str) -> str | None:
    """Return the first remote ref that can be resolved."""
    for ref in _candidate_history_refs(repo_path):
        ok, _ = run_git(
            ["rev-parse", "--verify", ref],
            cwd=repo_path,
            timeout=REPO_TIMEOUT,
        )

        if ok:
            return ref

    return None


# ----------------------------
# Commit checkout helpers
# ----------------------------
def checkout_commit_before_date(repo_path: str, date_str: str):
    """
    Check out the latest commit at or before a given date.

    The date should be formatted as YYYY-MM-DD. Returns the commit hash and an
    optional diagnostic message.
    """
    ref = _first_valid_ref(repo_path)

    if ref is None:
        return None, "rev-parse failed: no valid ref found"

    ok, out = run_git(
        ["rev-list", "-n", "1", f"--before={date_str}T23:59:59", ref],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    if not ok:
        return None, f"rev-list failed: {out}"

    commit = (out or "").strip()

    if not commit:
        ok2, first = run_git(
            ["log", "--reverse", "--pretty=%cI", "-n", "1", ref],
            cwd=repo_path,
            timeout=REPO_TIMEOUT,
        )

        if ok2 and (first or "").strip():
            return None, f"No commit exists before {date_str}; first_commit={first.strip()}"

        return None, f"No commit exists before {date_str}"

    # Disposable analysis clone: make sure the worktree is clean.
    run_git(
        ["reset", "--hard"],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    run_git(
        ["clean", "-xfd"],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    ok, out2 = run_git(
        [
            "-c", "filter.lfs.smudge=",
            "-c", "filter.lfs.process=",
            "-c", "filter.lfs.required=false",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "checkout",
            "--force",
            "--detach",
            commit,
        ],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    if not ok:
        msg = str(out2 or "")
        msg_lower = msg.lower()

        if "invalid path" in msg_lower:
            return None, f"checkout failed: invalid Windows path: {msg}"

        if "filename too long" in msg_lower or "path too long" in msg_lower:
            return None, f"checkout failed: path too long: {msg}"

        if "user interactivity disabled" in msg_lower:
            return None, f"checkout failed: non-interactive auth/lfs error: {msg}"

        return None, f"checkout failed: {msg}"

    return commit, None


def checkout_last_commit_within_elbow(
    repo_path: str,
    start_date: str,
    elbow_days: int = 11,
    timeout: int = 120,
):
    """
    Check out the latest commit in the post-event elbow window.

    The selected commit must be strictly after start_date and no later than
    start_date + elbow_days.
    """
    try:
        start_ts = datetime.strptime(start_date, "%Y-%m-%d")
        end_ts = start_ts + timedelta(days=elbow_days)

        cmd = [
            "git",
            "-C",
            repo_path,
            "log",
            "--all",
            "--format=%H%x09%cI",
            f"--after={start_ts.strftime('%Y-%m-%dT00:00:00')}",
            f"--before={end_ts.strftime('%Y-%m-%dT23:59:59')}",
        ]

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        if process.returncode != 0:
            return None, process.stderr.strip() or process.stdout.strip()

        candidates = []

        for line in process.stdout.splitlines():
            line = line.strip()

            if not line:
                continue

            try:
                commit_hash, commit_iso = line.split("\t", 1)
                commit_dt = datetime.fromisoformat(commit_iso.replace("Z", "+00:00"))
                commit_date_naive = commit_dt.replace(tzinfo=None)

            except Exception:
                continue

            elbow_end = end_ts.replace(hour=23, minute=59, second=59)

            if start_ts < commit_date_naive <= elbow_end:
                candidates.append((commit_date_naive, commit_hash))

        if not candidates:
            return None, f"No commit after {start_date} and within {elbow_days} elbow days"

        candidates.sort(key=lambda item: item[0])
        commit = candidates[-1][1]

        checkout = subprocess.run(
            ["git", "-C", repo_path, "checkout", "--force", commit],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

        if checkout.returncode != 0:
            return None, checkout.stderr.strip() or checkout.stdout.strip()

        return commit, ""

    except subprocess.TimeoutExpired:
        return None, f"git timeout while finding/checking out elbow commit after {start_date}"

    except Exception as e:
        return None, str(e)


def get_commit_date(repo_path: str, commit: str) -> str | None:
    """Return the commit date in ISO format."""
    ok, out = run_git(
        ["show", "-s", "--format=%cI", commit],
        cwd=repo_path,
        timeout=REPO_TIMEOUT,
    )

    if ok and (out or "").strip():
        return (out or "").strip()

    return None