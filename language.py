# language.py
from metrics_common import count_detectable_source_files


def detect_language(repo_path: str, exclude_generated: bool = True) -> str | None:
    """
    Detect the main analyzable language in a repository.

    Only Python and JavaScript/TypeScript are considered here because those
    are the languages supported by the metric extraction.
    """
    counts = count_detectable_source_files(
        repo_path,
        exclude_generated=exclude_generated,
    )

    py_count = counts["python_files"]
    js_count = counts["javascript_files"]

    if py_count == 0 and js_count == 0:
        return None

    if py_count > 0 and js_count == 0:
        return "python"

    if js_count > 0 and py_count == 0:
        return "javascript"

    return "python" if py_count >= js_count else "javascript"