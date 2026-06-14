# normalize.py
import re


GITHUB_REPO_URL_RE = re.compile(
    r"""
    (?P<url>
        (?:https?://)?
        github\.com/
        (?P<owner>[A-Za-z0-9_.-]+)/
        (?P<repo>[A-Za-z0-9_.-]+)
        (?:\.git)?
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def normalize_link(link) -> str | None:
    """Normalize a GitHub repository link to HTTPS clone format."""
    if not isinstance(link, str):
        return None

    link = link.strip()

    if not link:
        return None

    link = link.replace("www.", "")

    match = GITHUB_REPO_URL_RE.search(link)

    if not match:
        return None

    owner = match.group("owner")
    repo = match.group("repo")

    if repo.lower().endswith(".git"):
        repo = repo[:-4]

    return f"https://github.com/{owner}/{repo}.git"