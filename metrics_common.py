# metrics_common.py
import json
import os
import re
from pathlib import Path
from statistics import mean


# ----------------------------
# Exclusion rules
# ----------------------------
EXCLUDE_DIRS_ALWAYS = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".history",
    ".vs",
    ".svn",
    ".hg",
    "__MACOSX",
    ".metadata",
    "env",
    "Env",
    "ENV",

    # Dependency and vendored directories.
    "node_modules",
    "site-packages",
    "dist-packages",
    "__pypackages__",
    "bower_components",
    "jspm_packages",
    "vendor",
    "vendors",
    "third_party",
    "third-party",
}

EXCLUDE_DIRS_GENERATED = {
    # Build and generated output.
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".out",
    "coverage",
    "storybook-static",

    # Caches and tooling.
    ".cache",
    ".parcel-cache",
    ".turbo",

    # Codegen or compiled output.
    "generated",
    "gen",
    "dataconnect-generated",
    "target",
    "out",
    "release",
    "debug",
}

EXCLUDE_PATH_PATTERNS = [
    # Python environments and system installs.
    "/site-packages/",
    "/dist-packages/",
    "/__macosx/",
    "/.metadata/",
    "/system/library/frameworks/python.framework/",
    "/library/frameworks/python.framework/",
    "/versions/2.7/lib/python2.7/",
    "/versions/3.6/lib/python3.6/",
    "/versions/3.7/lib/python3.7/",
    "/versions/3.8/lib/python3.8/",
    "/versions/3.9/lib/python3.9/",
    "/versions/3.10/lib/python3.10/",
    "/versions/3.11/lib/python3.11/",
    "/versions/3.12/lib/python3.12/",
    "/lib/python2.7/",
    "/lib/python2/",
    "/lib/python3.6/",
    "/lib/python3.7/",
    "/lib/python3.8/",
    "/lib/python3.9/",
    "/lib/python3.10/",
    "/lib/python3.11/",
    "/lib/python3.12/",
    "/usr/lib/python2.7/",
    "/usr/lib/python/",
    "/cellar/python/",
    "/google-cloud-sdk/",

    # Vendored and dependency directories.
    "/node_modules/",
    "/vendor/",
    "/vendors/",
    "/third_party/",
    "/third-party/",
    "/bower_components/",
    "/jspm_packages/",
]


# ----------------------------
# File patterns
# ----------------------------
MINIFIED_MARKER_RE = re.compile(r"\.min\.(js|jsx|ts|tsx)$", re.IGNORECASE)
README_RE = re.compile(r"^readme(\..*)?$", re.IGNORECASE)

MAX_SOURCE_FILE_BYTES = 5_000_000

ALL_CODE_EXTENSIONS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
)


# ----------------------------
# JavaScript built-in modules
# ----------------------------
JS_BUILTIN_MODULES = {
    "assert",
    "assert/strict",
    "async_hooks",
    "buffer",
    "child_process",
    "cluster",
    "console",
    "constants",
    "crypto",
    "dgram",
    "diagnostics_channel",
    "dns",
    "dns/promises",
    "domain",
    "events",
    "fs",
    "fs/promises",
    "http",
    "http2",
    "https",
    "inspector",
    "inspector/promises",
    "module",
    "net",
    "os",
    "path",
    "path/posix",
    "path/win32",
    "perf_hooks",
    "process",
    "punycode",
    "querystring",
    "readline",
    "readline/promises",
    "repl",
    "stream",
    "stream/consumers",
    "stream/promises",
    "stream/web",
    "string_decoder",
    "sys",
    "timers",
    "timers/promises",
    "tls",
    "trace_events",
    "tty",
    "url",
    "util",
    "util/types",
    "v8",
    "vm",
    "wasi",
    "worker_threads",
    "zlib",
}


# ----------------------------
# Exclusion helpers
# ----------------------------
def is_excluded_path(full_path: str) -> bool:
    """Check path-level exclusions after normalizing separators."""
    norm = full_path.replace("\\", "/").lower()
    return any(token in norm for token in EXCLUDE_PATH_PATTERNS)


def is_excluded_dir(dirname: str, exclude_generated: bool = True) -> bool:
    """Check whether a directory should be skipped."""
    if dirname in EXCLUDE_DIRS_ALWAYS:
        return True

    if exclude_generated and dirname in EXCLUDE_DIRS_GENERATED:
        return True

    return False


def is_generated_filename(name_lower: str) -> bool:
    """Detect common generated or bundled source files."""
    return (
        name_lower.endswith((".min.js", ".min.jsx", ".min.ts", ".min.tsx"))
        or name_lower.endswith((".bundle.js", ".bundle.jsx", ".bundle.ts", ".bundle.tsx"))
        or name_lower.endswith(
            (
                ".generated.py",
                ".generated.js",
                ".generated.jsx",
                ".generated.ts",
                ".generated.tsx",
            )
        )
        or name_lower.endswith((".g.py", ".g.js", ".g.jsx", ".g.ts", ".g.tsx"))
        or name_lower.endswith((".pb.py", ".pb.js", ".pb.ts"))
        or name_lower.endswith((".designer.js", ".designer.ts"))
        or name_lower.endswith(".d.ts")
    )


def looks_binary_or_invalid_text(path: str, sample_size: int = 4096) -> bool:
    """Use a small byte sample to skip binary-looking files."""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(sample_size)

        if b"\x00" in chunk:
            return True

        # Crude control-character heuristic.
        bad = sum(byte < 9 or (13 < byte < 32) for byte in chunk)
        return len(chunk) > 0 and (bad / len(chunk)) > 0.30

    except Exception:
        return False


# ----------------------------
# Source file iteration
# ----------------------------
def iter_source_files(repo_path: str, lang: str, exclude_generated: bool = True):
    """Yield analyzable source files for Python or JavaScript/TypeScript."""
    exts = (".py",) if lang == "python" else (".js", ".jsx", ".ts", ".tsx")

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not is_excluded_dir(d, exclude_generated)]

        for file_name in files:
            if file_name.startswith("._"):
                continue

            name_lower = file_name.lower()

            if not name_lower.endswith(exts):
                continue

            full_path = os.path.join(root, file_name)

            if lang == "javascript" and MINIFIED_MARKER_RE.search(name_lower):
                continue

            if is_excluded_path(full_path):
                continue

            if is_generated_filename(name_lower):
                continue

            if looks_binary_or_invalid_text(full_path):
                continue

            try:
                if os.path.getsize(full_path) > MAX_SOURCE_FILE_BYTES:
                    continue

            except Exception:
                pass

            yield full_path


def count_detectable_source_files(repo_path: str, exclude_generated: bool = True) -> dict:
    """Count Python and JavaScript/TypeScript files after exclusions."""
    py_files = sum(
        1 for _ in iter_source_files(repo_path, "python", exclude_generated=exclude_generated)
    )
    js_files = sum(
        1 for _ in iter_source_files(repo_path, "javascript", exclude_generated=exclude_generated)
    )

    return {
        "python_files": py_files,
        "javascript_files": js_files,
    }


def count_all_source_files(repo_path: str, exclude_generated: bool = True) -> dict:
    """Count supported source files after exclusions."""
    py_files = list(iter_source_files(repo_path, "python", exclude_generated))
    js_files = list(iter_source_files(repo_path, "javascript", exclude_generated))

    total_files = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not is_excluded_dir(d, exclude_generated)]

        for file_name in files:
            name_lower = file_name.lower()

            if not name_lower.endswith(ALL_CODE_EXTENSIONS):
                continue

            full_path = os.path.join(root, file_name)

            if is_excluded_path(full_path):
                continue

            if is_generated_filename(name_lower):
                continue

            if looks_binary_or_invalid_text(full_path):
                continue

            try:
                if os.path.getsize(full_path) > MAX_SOURCE_FILE_BYTES:
                    continue

            except Exception:
                pass

            total_files += 1

    return {
        "python_files": len(py_files),
        "javascript_files": len(js_files),
        "all_source_files": total_files,
    }


# ----------------------------
# Basic statistics
# ----------------------------
def count_words(text: str) -> int:
    """Count whitespace-separated words."""
    return len([word for word in re.split(r"\s+", text.strip()) if word])


def safe_mean(values):
    """Return the mean, or None for empty values."""
    return float(mean(values)) if values else None


def safe_median(values):
    """Return the median, or None for empty values."""
    if not values:
        return None

    sorted_values = sorted(values)
    n = len(sorted_values)
    mid = n // 2

    if n % 2 == 1:
        return float(sorted_values[mid])

    return float((sorted_values[mid - 1] + sorted_values[mid]) / 2)


def safe_min(values):
    """Return the minimum, or None for empty values."""
    return float(min(values)) if values else None


def safe_max(values):
    """Return the maximum, or None for empty values."""
    return float(max(values)) if values else None


def percentile(values, p: float):
    """Return a simple interpolated percentile."""
    if not values:
        return None

    sorted_values = sorted(values)

    if len(sorted_values) == 1:
        return float(sorted_values[0])

    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)

    if f == c:
        return float(sorted_values[f])

    return float(sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f))


# ----------------------------
# README helpers
# ----------------------------
def has_readme(repo_path: str) -> bool:
    """Check whether the repository root contains a README file."""
    root = Path(repo_path)

    for path in root.iterdir():
        if path.is_file() and README_RE.match(path.name):
            return True

    return False


def readme_word_count(repo_path: str) -> int:
    """Count README words in the repository root."""
    root = Path(repo_path)

    for path in root.iterdir():
        if not (path.is_file() and README_RE.match(path.name)):
            continue

        try:
            text = path.read_text(encoding="utf8", errors="ignore")
            return count_words(text)

        except Exception:
            return 0

    return 0


# ----------------------------
# Dependency normalization
# ----------------------------
def normalize_python_pkg_name(name: str) -> str:
    """Normalize Python package names for comparison."""
    if not isinstance(name, str):
        return ""

    return name.strip().lower().replace("_", "-")


def normalize_js_pkg_name(dep: str) -> str:
    """Normalize a JavaScript dependency to package level."""
    if not isinstance(dep, str):
        return ""

    dep = dep.strip()

    if not dep:
        return ""

    if dep.startswith("@"):
        parts = dep.split("/")
        return "/".join(parts[:2]).lower() if len(parts) >= 2 else dep.lower()

    return dep.split("/")[0].lower()


def is_js_builtin_module(dep: str) -> bool:
    """Check whether a dependency is a built-in Node module."""
    if not isinstance(dep, str):
        return False

    dep = dep.strip().lower()

    if not dep:
        return False

    # Handle node:fs, node:path, etc.
    if dep.startswith("node:"):
        dep = dep[5:]

    return dep in JS_BUILTIN_MODULES


# ----------------------------
# Dependency file parsers
# ----------------------------
def parse_requirements_txt(path: str) -> set[str]:
    """Parse dependencies from requirements.txt."""
    deps = set()

    try:
        for line in open(path, encoding="utf8", errors="ignore"):
            line = line.strip()

            if not line or line.startswith("#") or line.startswith("-r") or line.startswith("--"):
                continue

            pkg = re.split(r"[<>=!~\s\[]", line, maxsplit=1)[0].strip()

            if pkg:
                deps.add(pkg.lower())

    except Exception:
        pass

    return deps


def parse_pyproject_toml(path: str) -> set[str]:
    """Parse dependencies from common pyproject.toml sections."""
    deps = set()

    try:
        text = open(path, encoding="utf8", errors="ignore").read()

    except Exception:
        return deps

    for section in ["tool.poetry.dependencies", "tool.poetry.dev-dependencies"]:
        match = re.search(rf"\[{re.escape(section)}\]\s*(.*?)(\n\[|\Z)", text, re.S)

        if not match:
            continue

        block = match.group(1)

        for line in block.splitlines():
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            name = line.split("=", 1)[0].strip().strip('"').strip("'")

            if name and name.lower() != "python":
                deps.add(name.lower())

    match = re.search(r"\[project\]\s*(.*?)(\n\[|\Z)", text, re.S)

    if match:
        block = match.group(1)
        dep_match = re.search(r"dependencies\s*=\s*\[(.*?)\]", block, re.S)

        if dep_match:
            arr = dep_match.group(1)

            for item in re.findall(r'"([^"]+)"', arr):
                pkg = re.split(r"[<>=!~\s\[]", item.strip(), maxsplit=1)[0]

                if pkg:
                    deps.add(pkg.lower())

    return deps


def parse_package_json_deps(path: str) -> set[str]:
    """Parse dependencies from package.json."""
    deps = set()

    try:
        data = json.loads(open(path, encoding="utf8", errors="ignore").read())

        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            obj = data.get(key) or {}

            if isinstance(obj, dict):
                for name in obj.keys():
                    deps.add(name)

    except Exception:
        pass

    return deps


def declared_deps(repo_path: str, lang: str) -> set[str]:
    """Collect declared dependencies for Python or JavaScript projects."""
    found = set()

    if lang == "python":
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if not is_excluded_dir(d, exclude_generated=True)]

            for file_name in files:
                name_lower = file_name.lower()

                if name_lower == "requirements.txt":
                    found |= parse_requirements_txt(os.path.join(root, file_name))

                elif name_lower == "pyproject.toml":
                    found |= parse_pyproject_toml(os.path.join(root, file_name))

        return found

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not is_excluded_dir(d, exclude_generated=True)]

        for file_name in files:
            if file_name.lower() == "package.json":
                found |= parse_package_json_deps(os.path.join(root, file_name))

    return found