# metrics_python.py
import ast
import sys
import os
import json
import re
import tokenize
from collections import defaultdict
from io import BytesIO

from metrics_common import (
    iter_source_files, declared_deps, readme_word_count,
    safe_mean, safe_median, safe_min, safe_max, percentile, count_words,
    normalize_python_pkg_name
)

def _precheck_python_source(text: str) -> str | None:
    if "<<<<<<<" in text and "=======" in text and ">>>>>>>" in text:
        return "Merge conflict marker encountered."
    if "\x00" in text:
        return "File appears to be binary."
    return None

def _is_probable_stdlib_module(name: str) -> bool:
    if not name:
        return False
    try:
        return name in getattr(sys, "stdlib_module_names", set())
    except Exception:
        return False
    
def _static_python_string(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Str):
        return node.s
    if isinstance(node, ast.JoinedStr):
        # only allow f-strings with no expressions
        if not getattr(node, "values", None):
            return ""
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.Str):
                parts.append(part.s)
            else:
                return None
        return "".join(parts)
    return None

def _classify_py_syntax_error(msg: str, text: str | None = None) -> str:
    m = (msg or "").lower()
    t = (text or "").strip()

    if "missing parentheses in call to 'print'" in m:
        return "py2_print"
    if "multiple exception types must be parenthesized" in m:
        return "py2_except"
    if "invalid syntax" in m and re.search(r"\basync\b", t):
        return "legacy_async_keyword"
    if "invalid syntax" in m and re.search(r"\bawait\b", t):
        return "legacy_await_keyword"
    if "invalid syntax" in m and re.search(r"\bmatch\b|\bcase\b", t):
        return "newer_python_syntax"
    if "expected an indented block" in m:
        return "indent"
    if "unexpected indent" in m:
        return "unexpected_indent"
    if "unindent does not match any outer indentation level" in m:
        return "bad_unindent"
    if "inconsistent use of tabs and spaces in indentation" in m:
        return "mixed_tabs_spaces"
    if "expected ':'" in m:
        return "missing_colon"
    if "unterminated triple-quoted string literal" in m:
        return "unterminated_triple_quote"
    if "unterminated string literal" in m or "eol while scanning string literal" in m:
        return "unterminated_string"
    if "invalid decimal literal" in m:
        return "invalid_decimal_literal"
    if "unicodeescape" in m:
        return "unicode_escape"
    if "invalid syntax" in m:
        return "invalid_syntax"
    return "syntax"

def _relpath_to_module(rel_path: str) -> str:
    mod = rel_path.replace("\\", "/").replace("/", ".")
    if mod.endswith(".py"):
        mod = mod[:-3]
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def _resolve_importfrom(src_module: str, node: ast.ImportFrom) -> str | None:
    """
    Resolve `from X import ...` to a module string.
    Handles relative imports using node.level.
    Returns resolved module or None.
    """
    mod = node.module  # may be None
    level = getattr(node, "level", 0) or 0

    # Absolute import
    if level == 0:
        return mod

    # Relative: compute base package from src_module
    # src_module like "a.b.c"; importing with level=1 means from ".": package is "a.b"
    parts = src_module.split(".") if src_module else []
    # for a module, relative imports are based on its package => drop last segment
    if parts:
        parts = parts[:-1]

    # go up (level-1) more
    up = max(0, level - 1)
    if up:
        parts = parts[:-up] if up <= len(parts) else []

    base = ".".join(parts) if parts else ""
    if mod:
        return f"{base}.{mod}" if base else mod
    return base or None

# -----------------------------
# SLOC + comment word count (Python)
# includes: # comments + docstrings (counted as comments)
# SLOC excludes blank + comment-only + docstring-only lines
# -----------------------------
def _py_sloc_and_comment_words(path: str):
    raw = open(path, "rb").read()
    text = raw.decode("utf8", errors="ignore")
    lines = text.splitlines()

    blank_lines = {i + 1 for i, line in enumerate(lines) if not line.strip()}

    # Track whether a line has code tokens and/or comment tokens
    has_code = defaultdict(bool)
    has_comment = defaultdict(bool)
    comment_words = 0

    try:
        for tok in tokenize.tokenize(BytesIO(raw).readline):
            ln = tok.start[0]

            if tok.type == tokenize.COMMENT:
                has_comment[ln] = True
                comment_words += count_words(tok.string.lstrip("#").strip())
                continue

            # Non-code structural tokens
            if tok.type in (
                tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
                tokenize.ENCODING, tokenize.ENDMARKER
            ):
                continue

            # Anything else counts as "code on that line"
            has_code[ln] = True
    except Exception:
        pass

    # Docstrings: count words + mark their lines as comment-only-ish for SLOC exclusion
    docstring_lines = set()
    try:
        tree = ast.parse(text, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if not body:
                    continue
                first = body[0]
                if isinstance(first, ast.Expr):
                    v = getattr(first, "value", None)
                    # Py3.8+: Constant(str); older: Str
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        ds = v.value
                    elif isinstance(v, ast.Str):
                        ds = v.s
                    else:
                        ds = None

                    if isinstance(ds, str):
                        s = getattr(first, "lineno", None)
                        e = getattr(first, "end_lineno", None) or s
                        if s:
                            for ln in range(s, (e or s) + 1):
                                docstring_lines.add(ln)
                        comment_words += count_words(ds)
    except Exception:
        pass

    # comment-only lines: have comment/docstring, and no code tokens on that line
    comment_only = {ln for ln, v in has_comment.items() if v and not has_code.get(ln, False)}
    # docstring lines should be excluded from SLOC even though they tokenize as STRING (code token)
    comment_only |= docstring_lines

    sloc = 0
    for i, line in enumerate(lines, start=1):
        if i in blank_lines:
            continue
        if i in comment_only:
            continue
        sloc += 1

    return sloc, comment_words, blank_lines, comment_only


# -----------------------------
# McCabe CC (strict-ish):
# 1 + decisions
# Decisions: if/for/while/except/ifexp, comprehensions (+ifs), boolean ops, match cases
# -----------------------------
CC_DECISION_NODES = (ast.If, ast.For, ast.While, ast.AsyncFor, ast.ExceptHandler, ast.IfExp)
BOOL_OPS = (ast.And, ast.Or)


def _is_wildcard_case(case: ast.match_case) -> bool:
    p = getattr(case, "pattern", None)
    return isinstance(p, ast.MatchAs) and getattr(p, "pattern", None) is None and getattr(p, "name", None) is None


def mccabe_cc(node: ast.AST) -> int:
    cc = 1
    for n in ast.walk(node):
        if isinstance(n, CC_DECISION_NODES):
            cc += 1
        elif isinstance(n, ast.BoolOp) and isinstance(n.op, BOOL_OPS):
            cc += max(0, len(getattr(n, "values", [])) - 1)
        elif isinstance(n, ast.comprehension):
            cc += 1
            cc += len(getattr(n, "ifs", []) or [])
        elif isinstance(n, ast.Match):
            cases = getattr(n, "cases", []) or []
            cc += sum(0 if _is_wildcard_case(c) else 1 for c in cases)
    return cc


def _py_lloc_functions_cc_imports_used(path: str, src_module: str | None, internal_modules: set[str] | None = None):

    """
    Returns:
      lloc,
      functions,
      cc_per_fn,
      used_import_sources,
      import_bindings_by_source,
      parse_ok (bool),
      parse_error
    """

    txt = open(path, "r", encoding="utf8", errors="ignore").read()
    precheck_result = _precheck_python_source(txt)
    if precheck_result:
        return 0, [], [], set(), {}, False, precheck_result

    try:
        tree = ast.parse(txt, filename=path)
    except SyntaxError as e:
        msg = getattr(e, "msg", None) or str(e)
        lineno = getattr(e, "lineno", None)
        offset = getattr(e, "offset", None)
        text = getattr(e, "text", None)

        detail = msg
        if lineno is not None:
            detail += f" (line {lineno}"
            if offset is not None:
                detail += f", col {offset}"
            detail += ")"
        if text:
            snippet = text.strip()
            if snippet:
                detail += f": {snippet[:200]}"
        tag = _classify_py_syntax_error(msg, text)
        return 0, [], [], set(), {}, False, f"[{tag}] {detail}"
    except Exception as e:
        return 0, [], [], set(), {}, False, str(e)

    lloc = sum(1 for n in ast.walk(tree) if isinstance(n, ast.stmt))

    # binding name -> candidate source modules
    binding_to_sources = defaultdict(set)
    importlib_module_bindings = {"importlib"}
    import_module_func_bindings = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                src = alias.name
                local = alias.asname or alias.name.split(".")[0]
                binding_to_sources[local].add(src)

                # Track aliases for importlib module
                if src == "importlib":
                    importlib_module_bindings.add(local)

        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_importfrom(src_module or "", node)
            if not resolved:
                continue

            for alias in node.names:
                if alias.name == "*":
                    binding_to_sources["*"].add(resolved)
                    continue

                local = alias.asname or alias.name

                # Base candidate: the import-from module itself
                binding_to_sources[local].add(resolved)

                # More specific candidate: resolved + imported name
                binding_to_sources[local].add(f"{resolved}.{alias.name}")

                # Track direct imports of import_module
                if resolved == "importlib" and alias.name == "import_module":
                    import_module_func_bindings.add(local)

    # Static dynamic-import detection
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # __import__("pkg.mod")
        if isinstance(node.func, ast.Name) and node.func.id == "__import__":
            if node.args:
                s = _static_python_string(node.args[0])
                if s:
                    binding_to_sources["__dynamic_import__"].add(s)
            continue

        # importlib.import_module("pkg.mod") or il.import_module("pkg.mod")
        if isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_module_bindings
            ):
                if node.args:
                    s = _static_python_string(node.args[0])
                    if s:
                        binding_to_sources["__dynamic_import__"].add(s)
                continue

        # from importlib import import_module
        # import_module("pkg.mod") or aliased version
        if isinstance(node.func, ast.Name) and node.func.id in import_module_func_bindings:
            if node.args:
                s = _static_python_string(node.args[0])
                if s:
                    binding_to_sources["__dynamic_import__"].add(s)
            continue

    # collect used names
    used_names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Name):
            used_names.add(node.id)

    used_sources = set()

    for local, candidates in binding_to_sources.items():
        if local not in {"*", "__dynamic_import__"} and local not in used_names:
            continue

        candidates = {c for c in candidates if isinstance(c, str) and c}

        if not candidates:
            continue

        # Prefer the most specific candidate that is actually internal
        if internal_modules:
            internal_candidates = [c for c in candidates if c in internal_modules]
            if internal_candidates:
                used_sources.add(max(internal_candidates, key=len))
                continue

        # Otherwise prefer the shortest candidate as the conceptual source
        # e.g. extlib instead of extlib.tool
        used_sources.add(min(candidates, key=len))

    # reconstruct old-style view for compatibility/debugging
    imports_by_source = defaultdict(set)
    for local, candidates in binding_to_sources.items():
        for src in candidates:
            imports_by_source[src].add(local)

    # functions: defs + async defs + lambdas
    functions = []
    cc_vals = []

    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            s = getattr(fn, "lineno", None)
            e = getattr(fn, "end_lineno", None)
            if s is None:
                continue
            if e is None:
                e = s
            functions.append((s, e, fn))
            cc_vals.append(mccabe_cc(fn))

    return lloc, functions, cc_vals, used_sources, dict(imports_by_source), True, None

def extract_metrics(repo_path: str, exclude_generated: bool = True) -> tuple[dict, list[dict]]:
    files = list(iter_source_files(repo_path, "python", exclude_generated=exclude_generated))

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

    internal_modules = set()
    rel_to_module = {}
    for f in files:
        rel = os.path.relpath(f, repo_path).replace("\\", "/")
        mod = _relpath_to_module(rel)
        internal_modules.add(mod)
        rel_to_module[rel] = mod

    graph = defaultdict(set)
    external_static = set()

    for f in files:
        rel = os.path.relpath(f, repo_path).replace("\\", "/")
        src_module = rel_to_module.get(rel)

        try:
            sloc, comment_words, blank_lines, comment_only_lines = _py_sloc_and_comment_words(f)
        except Exception:
            sloc, comment_words = 0, 0
            blank_lines, comment_only_lines = set(), set()

        total_sloc += sloc
        total_comment_words += comment_words
        per_file_sloc.append(sloc)

        lloc, functions, cc_vals, used_sources, imports_by_source, parse_ok, parse_error = _py_lloc_functions_cc_imports_used(
            f,
            src_module,
            internal_modules=internal_modules,
        )

        if not parse_ok:
            parse_fail_files += 1
            per_file_records.append({
                "rel_path": rel,
                "sloc": sloc,
                "lloc": 0,
                "functions": 0,
                "cc_total": 0,
                "cc_mean_per_function": None,
                "comment_words": comment_words,
                "fan_in": 0,
                "fan_out": 0,
                "parse_ok": 0,
                "parse_error": parse_error or "[python_parse_failed]",
            })
            continue

        parse_ok_files += 1

        total_lloc += lloc
        per_file_lloc.append(lloc)

        total_functions += len(functions)
        per_fn_cc.extend(cc_vals or [])

        for (s, e, _node) in functions:
            if not isinstance(s, int):
                continue
            if not isinstance(e, int):
                e = s
            if e < s:
                continue

            fn_sloc = 0
            for ln in range(s, e + 1):
                if ln in blank_lines or ln in comment_only_lines:
                    continue
                fn_sloc += 1
            per_fn_sloc.append(fn_sloc)

        if src_module:
            for src in (used_sources or set()):
                base = src.split(".")[0]
                if src in internal_modules:
                    graph[src_module].add(src)
                elif base in internal_modules:
                    graph[src_module].add(base)
                else:
                    norm_base = normalize_python_pkg_name(base)
                    if norm_base and not _is_probable_stdlib_module(norm_base):
                        external_static.add(norm_base)
                        
        per_file_records.append({
            "rel_path": rel,
            "sloc": sloc,
            "lloc": lloc,
            "functions": len(functions),
            "cc_total": sum(cc_vals) if cc_vals else 0,
            "cc_mean_per_function": safe_mean(cc_vals) if cc_vals else None,
            "comment_words": comment_words,
            "fan_in": 0,
            "fan_out": 0,
            "parse_ok": 1,
            "parse_error": None,
        })

    inbound = defaultdict(set)
    for src, deps in graph.items():
        for d in deps:
            inbound[d].add(src)

    fan_in_list = []
    fan_out_list = []
    for m in sorted(internal_modules):
        fan_out_list.append(len(graph.get(m, set())))
        fan_in_list.append(len(inbound.get(m, set())))

    mod_to_idx = {}
    for i, rec in enumerate(per_file_records):
        mod = _relpath_to_module(rec["rel_path"])
        mod_to_idx[mod] = i

    for mod, i in mod_to_idx.items():
        per_file_records[i]["fan_out"] = len(graph.get(mod, set()))
        per_file_records[i]["fan_in"] = len(inbound.get(mod, set()))

    declared_raw = declared_deps(repo_path, "python")
    declared = {
        normalize_python_pkg_name(x)
        for x in declared_raw
        if isinstance(x, str)
        and x
        and not _is_probable_stdlib_module(normalize_python_pkg_name(x))
    }
    external_union = set(external_static) | set(declared)

    readme_words = readme_word_count(repo_path)

    function_density_per_sloc = (total_functions / total_sloc) if total_sloc else None
    function_density_per_lloc = (total_functions / total_lloc) if total_lloc else None

    cc_total = int(sum(per_fn_cc)) if per_fn_cc else 0
    cc_density_per_sloc = (cc_total / total_sloc) if total_sloc else None
    cc_density_per_lloc = (cc_total / total_lloc) if total_lloc else None

    comment_words_density_per_sloc = (total_comment_words / total_sloc) if total_sloc else None
    comment_words_density_per_lloc = (total_comment_words / total_lloc) if total_lloc else None

    ext_union_count = len(external_union)
    external_deps_union_density_per_sloc = (ext_union_count / total_sloc) if total_sloc else None
    external_deps_union_density_per_lloc = (ext_union_count / total_lloc) if total_lloc else None

    parse_total = parse_ok_files + parse_fail_files
    parse_fail_ratio = (parse_fail_files / parse_total) if parse_total else None

    suspect_all_zero = 1 if (
        len(files) > 0 and total_sloc == 0 and total_lloc == 0 and total_functions == 0
    ) else 0

    metrics = {
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
        "external_deps_union_count": ext_union_count,

        "external_deps_union_density_per_sloc": external_deps_union_density_per_sloc,
        "external_deps_union_density_per_lloc": external_deps_union_density_per_lloc,

        "parse_ok_files": parse_ok_files,
        "parse_fail_files": parse_fail_files,
        "parse_fail_ratio": parse_fail_ratio,
        "suspect_all_zero": suspect_all_zero,
    }

    return metrics, per_file_records