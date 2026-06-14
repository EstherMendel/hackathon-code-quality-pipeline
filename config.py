# config.py
from pathlib import Path


# ----------------------------
# Paths
# ----------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR = PROJECT_ROOT / "Data"
REPOS_DIR = Path(r"C:\repos")

DB_PATH = DATA_DIR / "dataset_filtered.db"
FILES_DB_PATH = DATA_DIR / "metrics_files.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
REPOS_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------------
# JavaScript import aliases
# ----------------------------
JS_ALIAS_PREFIXES = {
    "@/": ["src/", "", "app/"],
    "~/": ["src/", "", "app/"],
}


# ----------------------------
# Runtime settings
# ----------------------------
REPO_TIMEOUT = 60 * 60
TIMEOUT_METRICS = 60 * 60

CHECKPOINT_EVERY = 5
COMMIT_EVERY = 5
MAX_WORKERS = 6


# ----------------------------
# Debug flags
# ----------------------------
DEBUG_JS = 1