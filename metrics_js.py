# metrics_js.py
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from config import DEBUG_JS, JS_ALIAS_PREFIXES, TIMEOUT_METRICS
from metrics_common import (
    declared_deps,
    is_js_builtin_module,
    iter_source_files,
    normalize_js_pkg_name,
    percentile,
    readme_word_count,
    safe_max,
    safe_mean,
    safe_median,
    safe_min,
)


# ----------------------------
# Node analyzer
# ----------------------------
JS_ANALYZER_PATH = Path(__file__).resolve().parent / "js_analyzer.js"


def _run_node_analyze(path: str) -> dict:
    """Run the Node analyzer for one JavaScript/TypeScript file."""
    try:
        proc = subprocess.run(
            ["node", str(JS_ANALYZER_PATH), path],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_METRICS,
        )

    except Exception as e:
        return {"ok": False, "error": f"node_run_fail: {e}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"node_exit_{proc.returncode}: {(proc.stderr or '')[:500]}",
        }

    out = (proc.stdout or "").strip()

    if not out:
        return {"ok": False, "error": "no_stdout"}

    try:
        return json.loads(out)

    except Exception:
        return {
            "ok": False,
            "error": "bad_json",
            "raw_stdout": out[:1000],
            "raw_stderr": (proc.stderr or "")[:1000],
        }


# ----------------------------
# Lexical fallback metrics
# ----------------------------
def _js_sloc_and_comment_words(path: str) -> tuple[int, int]:
    """
    Compute JS/TS SLOC and comment words without AST parsing.

    This keeps basic lexical metrics available even when the Node parser fails.
    """
    try:
        text = open(path, "r", encoding="utf8", errors="ignore").read()
    except Exception:
        return 0, 0

    lines = text.splitlines()

    blank = set()
    for i, line in enumerate(lines, start=1):
        if not line.strip():
            blank.add(i)

    comment_only = set()

    def strip_line_comments(line: str) -> str:
        idx = line.find("//")
        return line[:idx] if idx >= 0 else line

    def strip_block_comments_single_line(line: str) -> str:
        import re

        return re.sub(r"/\*.*?\*/", "", line)

    for i, original in enumerate(lines, start=1):
        trimmed = original.strip()

        if not trimmed:
            continue

        if trimmed.startswith("//"):
            comment_only.add(i)
            continue

        if trimmed.startswith("/*") or trimmed.startswith("*") or trimmed.startswith("*/"):
            comment_only.add(i)
            continue

        no_inline = strip_line_comments(strip_block_comments_single_line(original)).strip()

        if not no_inline:
            comment_only.add(i)

    sloc = 0

    for i in range(1, len(lines) + 1):
        if i in blank:
            continue

        if i in comment_only:
            continue

        sloc += 1

    def word_count(text_value: str) -> int:
        return len([x for x in (text_value or "").strip().split() if x])

    comment_words = 0

    for line in lines:
        idx = line.find("//")

        if idx >= 0:
            rhs = line[idx + 2 :]
            comment_words += word_count(rhs)

    import re

    block_matches = re.findall(r"/\*[\s\S]*?\*/", text)

    for block in block_matches:
        inner = block[2:-2]
        comment_words += word_count(inner)

    return sloc, comment_words


# ----------------------------
# Import normalization
# ----------------------------
def _normalize_js_import(
    dep: str,
    current_rel: str,
    internal_modules: set[str] | None = None,
) -> str | None:
    """Resolve JS/TS imports to internal module paths where possible."""
    if not isinstance(dep, str) or not dep:
        return None

    def strip_ext(path_value: str) -> str:
        path_value = path_value.replace("\\", "/").rstrip("/")

        for ext in (".ts", ".tsx", ".js", ".jsx"):
            if path_value.endswith(ext):
                return path_value[: -len(ext)]

        return path_value

    def existing_internal_candidate(candidates: list[str]) -> str | None:
        if not internal_modules:
            return None

        seen = set()

        for candidate in candidates:
            candidate = strip_ext(candidate)

            if candidate in seen:
                continue

            seen.add(candidate)

            if candidate in internal_modules:
                return candidate

            if (candidate + "/index") in internal_modules:
                return candidate + "/index"

        return None

    def resolve_relative_like(pathish: str) -> str | None:
        if not pathish.startswith(".") and not pathish.startswith("/"):
            return None

        current_dir = os.path.dirname(current_rel).replace("\\", "/")

        if pathish.startswith("/"):
            joined = pathish.lstrip("/")
        else:
            parts = [part for part in current_dir.split("/") if part]

            for segment in pathish.split("/"):
                if segment in ("", "."):
                    continue

                if segment == "..":
                    if parts:
                        parts.pop()
                else:
                    parts.append(segment)

            joined = "/".join(parts)

        return strip_ext(joined)

    # Standard relative or absolute import.
    direct = resolve_relative_like(dep)

    if direct is not None:
        return direct

    # Configured alias import, for example "@/components/Button".
    for alias, replacements in JS_ALIAS_PREFIXES.items():
        if not dep.startswith(alias):
            continue

        if isinstance(replacements, str):
            replacements = [replacements]

        suffix = dep[len(alias) :].lstrip("/")

        candidates = []

        for replacement in replacements:
            replacement = (replacement or "").replace("\\", "/")

            if replacement and not replacement.endswith("/"):
                replacement += "/"

            candidates.append(f"{replacement}{suffix}" if replacement else suffix)

        hit = existing_internal_candidate(candidates)

        if hit is not None:
            return hit

        # Do not force alias imports to be internal if no file matches.
        return None

    # External package.
    return None


# ----------------------------
# Per-file helpers
# ----------------------------
def _build_internal_module_index(files: list[str], repo_path: str) -> tuple[set[str], dict[str, str]]:
    """Map full paths to relative paths and known internal modules."""
    internal_modules = set()
    rel_by_full = {}

    for file_path in files:
        rel_path = os.path.relpath(file_path, repo_path).replace("\\", "/")
        rel_by_full[file_path] = rel_path
        internal_modules.add(rel_path.rsplit(".", 1)[0])

    return internal_modules, rel_by_full


def _empty_parse_failed_record(
    rel_path: str,
    sloc: int,
    comment_words: int,
    error: str,
) -> dict:
    """Create a per-file record for files where AST extraction failed."""
    return {
        "rel_path": rel_path,
        "sloc": sloc,
        "lloc": 0,
        "functions": 0,
        "cc_total": 0,
        "cc_mean_per_function": None,
        "comment_words": comment_words,
        "fan_in": 0,
        "fan_out": 0,
        "parse_ok": 0,
        "parse_error": error,
    }


def _file_record_from_node_result(
    rel_path: str,
    sloc: int,
    lloc: int,
    comment_words: int,
    functions_count: int,
    cc_vals: list,
    parse_mode_used,
    source_type_used,
    jsx_mode_used,
    parse_ok: bool,
    parse_error,
) -> dict:
    """Create a per-file record from a successful Node analyzer response."""
    if not parse_ok:
        if parse_error is None or str(parse_error).strip() == "":
            parse_error = "[js_parse_failed_unknown]"
    else:
        parse_error = None

    return {
        "rel_path": rel_path,
        "sloc": sloc,
        "lloc": lloc,
        "functions": functions_count,
        "cc_total": sum(int(x) for x in cc_vals) if cc_vals else 0,
        "cc_mean_per_function": safe_mean([int(x) for x in cc_vals]) if cc_vals else None,
        "comment_words": comment_words,
        "fan_in": 0,
        "fan_out": 0,
        "parse_mode_used": parse_mode_used,
        "source_type_used": source_type_used,
        "jsx_mode_used": 1 if jsx_mode_used is True else 0 if jsx_mode_used is False else None,
        "parse_ok": 1 if parse_ok else 0,
        "parse_error": parse_error,
    }


def _add_dependency_to_graph_or_external(
    dep: str,
    rel_path: str,
    src_mod: str,
    graph,
    external_static: set[str],
    internal_modules: set[str],
) -> None:
    """Add an import to the internal graph or external dependency set."""
    internal = _normalize_js_import(dep, rel_path, internal_modules=internal_modules)

    if internal:
        if internal in internal_modules:
            graph[src_mod].add(internal)
            return

        if (internal + "/index") in internal_modules:
            graph[src_mod].add(internal + "/index")
            return

    if isinstance(dep, str) and dep and not dep.startswith("."):
        norm_dep = normalize_js_pkg_name(dep)

        if norm_dep and not is_js_builtin_module(dep):
            external_static.add(norm_dep)


def _add_function_sloc_from_spans(fn_spans: list, blank: set[int], comment_only: set[int], per_fn_sloc: list) -> None:
    """Add function lengths based on line spans."""
    for span in fn_spans:
        if not (isinstance(span, (list, tuple)) and len(span) == 2):
            continue

        start, end = span

        try:
            start = int(start)
            end = int(end)
        except Exception:
            continue

        if end < start:
            continue

        fn_sloc = 0

        for line_number in range(start, end + 1):
            if line_number in blank or line_number in comment_only:
                continue

            fn_sloc += 1

        per_fn_sloc.append(fn_sloc)


def _add_cc_values(cc_vals: list, per_fn_cc: list) -> None:
    """Add per-function cyclomatic complexity values."""
    for value in cc_vals:
        try:
            per_fn_cc.append(int(value))
        except Exception:
            pass


def _add_fan_in_out_to_file_records(
    per_file_records: list[dict],
    graph,
    internal_modules: set[str],
) -> tuple[list[int], list[int]]:
    """Compute fan-in/fan-out lists and update per-file records."""
    inbound = defaultdict(set)

    for src, deps in graph.items():
        for dep in deps:
            inbound[dep].add(src)

    fan_in_list = []
    fan_out_list = []

    for module in sorted(internal_modules):
        fan_out_list.append(len(graph.get(module, set())))
        fan_in_list.append(len(inbound.get(module, set())))

    mod_to_idx = {}

    for i, record in enumerate(per_file_records):
        mod_to_idx[record["rel_path"].rsplit(".", 1)[0]] = i

    for module, i in mod_to_idx.items():
        per_file_records[i]["fan_out"] = len(graph.get(module, set()))
        per_file_records[i]["fan_in"] = len(inbound.get(module, set()))

    return fan_in_list, fan_out_list


# ----------------------------
# Metric aggregation
# ----------------------------
def _final_metrics(
    files: list[str],
    repo_path: str,
    total_sloc: int,
    total_lloc: int,
    total_comment_words: int,
    total_functions: int,
    per_file_sloc: list[int],
    per_file_lloc: list[int],
    per_fn_sloc: list[int],
    per_fn_cc: list[int],
    fan_in_list: list[int],
    fan_out_list: list[int],
    external_static: set[str],
    parse_ok_files: int,
    parse_fail_files: int,
) -> dict:
    """Aggregate repository-level metrics from per-file values."""
    declared_raw = declared_deps(repo_path, "javascript")

    declared = {
        normalize_js_pkg_name(dep)
        for dep in declared_raw
        if isinstance(dep, str)
        and dep
        and not is_js_builtin_module(dep)
    }

    external_union = set(external_static) | set(declared)

    readme_words = readme_word_count(repo_path)

    function_density_per_sloc = (total_functions / total_sloc) if total_sloc else None
    function_density_per_lloc = (total_functions / total_lloc) if total_lloc else None

    cc_total = sum(per_fn_cc) if per_fn_cc else 0
    cc_density_per_sloc = (cc_total / total_sloc) if total_sloc else None
    cc_density_per_lloc = (cc_total / total_lloc) if total_lloc else None

    comment_words_density_per_sloc = (
        total_comment_words / total_sloc
    ) if total_sloc else None
    comment_words_density_per_lloc = (
        total_comment_words / total_lloc
    ) if total_lloc else None

    ext_union_count = len(external_union)
    external_deps_union_density_per_sloc = (
        ext_union_count / total_sloc
    ) if total_sloc else None
    external_deps_union_density_per_lloc = (
        ext_union_count / total_lloc
    ) if total_lloc else None

    parse_total = parse_ok_files + parse_fail_files
    parse_fail_ratio = (parse_fail_files / parse_total) if parse_total else None

    suspect_all_zero = 1 if (
        len(files) > 0
        and total_sloc == 0
        and total_lloc == 0
        and total_functions == 0
    ) else 0

    return {
        "files_count": len(files),
        "readme_words": readme_words,

        "lloc_total": total_lloc,
        "sloc_total": total_sloc,

        "sloc_mean_per_file": safe_mean(per_file_sloc),
        "sloc_median_per_file": safe_median(per_file_sloc),
        "sloc_min_per_file": safe_min(per_file_sloc),
        "sloc_max_per_file": safe_max(per_file_sloc),

        "functions_total": total_functions,

        "function_length_sloc_mean": safe_mean(per_fn_sloc),
        "function_length_sloc_median": safe_median(per_fn_sloc),
        "function_length_sloc_min": safe_min(per_fn_sloc),
        "function_length_sloc_max": safe_max(per_fn_sloc),

        "function_density_per_sloc": function_density_per_sloc,
        "function_density_per_lloc": function_density_per_lloc,

        "cc_total": cc_total,
        "cc_mean_per_function": safe_mean(per_fn_cc),
        "cc_median_per_function": safe_median(per_fn_cc),
        "cc_min_per_function": safe_min(per_fn_cc),
        "cc_max_per_function": safe_max(per_fn_cc),

        "cc_density_per_sloc": cc_density_per_sloc,
        "cc_density_per_lloc": cc_density_per_lloc,

        "comment_words_total": total_comment_words,
        "comment_words_density_per_sloc": comment_words_density_per_sloc,
        "comment_words_density_per_lloc": comment_words_density_per_lloc,

        "fan_in_mean": safe_mean(fan_in_list),
        "fan_in_median": safe_median(fan_in_list),
        "fan_in_min": safe_min(fan_in_list),
        "fan_in_max": safe_max(fan_in_list),
        "fan_in_p95": percentile(fan_in_list, 0.95),

        "fan_out_mean": safe_mean(fan_out_list),
        "fan_out_median": safe_median(fan_out_list),
        "fan_out_min": safe_min(fan_out_list),
        "fan_out_max": safe_max(fan_out_list),
        "fan_out_p95": percentile(fan_out_list, 0.95),

        "fan_in_list_json": json.dumps(fan_in_list),
        "fan_out_list_json": json.dumps(fan_out_list),

        "external_deps_static_count": len(external_static),
        "external_deps_declared_count": len(declared),
        "external_deps_union_count": len(external_union),

        "external_deps_union_density_per_sloc": external_deps_union_density_per_sloc,
        "external_deps_union_density_per_lloc": external_deps_union_density_per_lloc,

        "parse_ok_files": parse_ok_files,
        "parse_fail_files": parse_fail_files,
        "parse_fail_ratio": parse_fail_ratio,
        "suspect_all_zero": suspect_all_zero,
    }


# ----------------------------
# Public extraction function
# ----------------------------
def extract_metrics(repo_path: str, exclude_generated: bool = True) -> tuple[dict, list[dict]]:
    """Extract repository-level and per-file JS/TS metrics."""
    files = list(
        iter_source_files(
            repo_path,
            "javascript",
            exclude_generated=exclude_generated,
        )
    )

    per_file_records = []

    per_file_sloc = []
    per_file_lloc = []
    per_fn_sloc = []
    per_fn_cc = []

    total_sloc = 0
    total_lloc = 0
    total_comment_words = 0
    total_functions = 0

    parse_ok_files = 0
    parse_fail_files = 0

    internal_modules, rel_by_full = _build_internal_module_index(files, repo_path)

    graph = defaultdict(set)
    external_static = set()

    mismatch_examples = 0

    for file_path in files:
        rel_path = rel_by_full[file_path]

        lexical_sloc, lexical_comment_words = _js_sloc_and_comment_words(file_path)
        data = _run_node_analyze(file_path)

        if not data.get("ok"):
            parse_fail_files += 1

            total_sloc += lexical_sloc
            total_comment_words += lexical_comment_words
            per_file_sloc.append(lexical_sloc)

            error = data.get("error") or "node_analyze_failed"

            if DEBUG_JS:
                print(f"[JS][ERROR] {rel_path}: {error}")

            per_file_records.append(
                _empty_parse_failed_record(
                    rel_path=rel_path,
                    sloc=lexical_sloc,
                    comment_words=lexical_comment_words,
                    error=error,
                )
            )

            continue

        sloc = int(data.get("sloc", lexical_sloc) or 0)
        lloc = int(data.get("lloc", 0) or 0)
        comment_words = int(data.get("commentWords", lexical_comment_words) or 0)

        total_sloc += sloc
        total_lloc += lloc
        total_comment_words += comment_words

        per_file_sloc.append(sloc)
        per_file_lloc.append(lloc)

        if data.get("parse_ok", True):
            parse_ok_files += 1
        else:
            parse_fail_files += 1

        parse_mode_used = data.get("parseModeUsed")
        source_type_used = data.get("sourceTypeUsed")
        jsx_mode_used = data.get("jsxModeUsed")

        fn_spans = data.get("fnSpans") or []
        cc_vals = data.get("ccVals") or []
        used_sources = data.get("usedImportSources") or []

        functions_count = int(data.get("functionsCount", len(cc_vals)) or 0)
        functions_with_loc = int(data.get("functionsWithLoc", len(fn_spans)) or 0)

        total_functions += functions_count

        if DEBUG_JS and mismatch_examples < 10:
            if len(cc_vals) != functions_count or functions_with_loc != len(fn_spans):
                print(
                    f"[JS][MISMATCH] {rel_path}: "
                    f"functionsCount={functions_count}, "
                    f"ccVals={len(cc_vals)}, "
                    f"spans={len(fn_spans)}"
                )
                mismatch_examples += 1

        blank = set(
            int(x)
            for x in (data.get("blankLines") or [])
            if str(x).isdigit()
        )
        comment_only = set(
            int(x)
            for x in (data.get("commentOnlyLines") or [])
            if str(x).isdigit()
        )

        _add_function_sloc_from_spans(
            fn_spans=fn_spans,
            blank=blank,
            comment_only=comment_only,
            per_fn_sloc=per_fn_sloc,
        )

        _add_cc_values(cc_vals=cc_vals, per_fn_cc=per_fn_cc)

        src_mod = rel_path.rsplit(".", 1)[0]

        for dep in used_sources:
            _add_dependency_to_graph_or_external(
                dep=dep,
                rel_path=rel_path,
                src_mod=src_mod,
                graph=graph,
                external_static=external_static,
                internal_modules=internal_modules,
            )

        parse_ok = bool(data.get("parse_ok", True))
        parse_error = data.get("parse_error")

        per_file_records.append(
            _file_record_from_node_result(
                rel_path=rel_path,
                sloc=sloc,
                lloc=lloc,
                comment_words=comment_words,
                functions_count=functions_count,
                cc_vals=cc_vals,
                parse_mode_used=parse_mode_used,
                source_type_used=source_type_used,
                jsx_mode_used=jsx_mode_used,
                parse_ok=parse_ok,
                parse_error=parse_error,
            )
        )

    fan_in_list, fan_out_list = _add_fan_in_out_to_file_records(
        per_file_records=per_file_records,
        graph=graph,
        internal_modules=internal_modules,
    )

    metrics = _final_metrics(
        files=files,
        repo_path=repo_path,
        total_sloc=total_sloc,
        total_lloc=total_lloc,
        total_comment_words=total_comment_words,
        total_functions=total_functions,
        per_file_sloc=per_file_sloc,
        per_file_lloc=per_file_lloc,
        per_fn_sloc=per_fn_sloc,
        per_fn_cc=per_fn_cc,
        fan_in_list=fan_in_list,
        fan_out_list=fan_out_list,
        external_static=external_static,
        parse_ok_files=parse_ok_files,
        parse_fail_files=parse_fail_files,
    )

    return metrics, per_file_records