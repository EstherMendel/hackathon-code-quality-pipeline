# cleanup_utils.py
import os
import shutil
import stat
import time
from pathlib import Path


def _on_rm_error(func, path, exc_info) -> None:
    """
    Handle rmtree errors caused by read-only files.

    This is mainly needed on Windows, where checked-out repository files may
    keep read-only flags.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def robust_rmtree(path: str | Path, retries: int = 6, delay: float = 0.5) -> bool:
    """
    Remove a directory with retries.

    This handles common Windows cleanup issues, such as read-only files or
    temporary file locks from antivirus, indexers, or node processes.
    """
    path = Path(path)

    if not path.exists():
        return True

    for attempt in range(retries):
        try:
            shutil.rmtree(path, onerror=_on_rm_error)
        except Exception:
            pass

        if not path.exists():
            return True

        # Small backoff between retries.
        time.sleep(delay * (attempt + 1))

    return not path.exists()