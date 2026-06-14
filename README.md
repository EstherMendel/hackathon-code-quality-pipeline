# Repository Metric Extraction Pipeline

This repository contains the code used for the repository-level metric extraction part of the master's thesis:

**Code Quality in Hackathon Projects: Status, Predictors, and Implications for Continuation and Reuse**

The pipeline is used to reconstruct repository snapshots, extract static code metrics, and store the results in SQLite databases for later statistical analysis.

## What this code does

The pipeline reads project records from a SQLite database, normalizes GitHub repository links, clones repositories, checks out the relevant historical commit, detects the main supported programming language, and extracts static code metrics.

The current implementation supports:

* Python
* JavaScript / TypeScript

For each repository, the pipeline extracts repository-level metrics such as source lines of code, logical lines of code, function counts, cyclomatic complexity, comments, dependency counts, fan-in/fan-out, parse success rates, and README length.

The pipeline can also extract optional **elbow-window metrics**, which describe short-term post-event repository activity. These metrics are stored with the `elbow_` prefix.

## Repository structure

The main files are:

* `main.py` — entry point for running the pipeline.
* `pipeline.py` — coordinates repository cloning, checkout, metric extraction, progress tracking, and database writes.
* `db.py` — defines the SQLite database schemas and write/update logic.
* `metrics_python.py` — extracts static metrics from Python files.
* `metrics_js.py` — extracts static metrics from JavaScript/TypeScript files.
* `js_analyzer.js` — Node-based AST analyzer used by `metrics_js.py`.
* `metrics_common.py` — shared helper functions for file filtering, dependency parsing, README counting, and summary statistics.
* `git_utils.py` — Git clone, branch, commit, and checkout helpers.
* `normalize.py` — normalizes GitHub repository links.
* `language.py` — detects whether a repository is mainly Python or JavaScript/TypeScript.
* `cleanup_utils.py` — removes repository folders robustly, mainly for Windows.
* `config.py` — stores paths and runtime settings.

## Requirements

### System requirements

The pipeline assumes the following software is available:

* Python 3.10 or newer
* Git
* Node.js
* SQLite

Git should be installed and available on the system PATH. On Windows, the code also checks common Git installation locations.

### Python packages

Install the Python dependencies with:

```bash
pip install -r requirements.txt
```

The basic pipeline requirements are listed in `requirements.txt`. These include packages for data handling, progress bars, and SQLite/database interaction through pandas.

### Node packages

The JavaScript/TypeScript analyzer uses Node.js and requires the dependencies listed in `package.json`.

Install them with:

```bash
npm install
```

This installs the parser used by `js_analyzer.js`:

```text
@typescript-eslint/typescript-estree
```

The file `js_analyzer.js` should be located in the same folder as `metrics_js.py`.

### Optional reproducibility files

For the final thesis archive, it is useful to keep locked dependency versions as well.

For Python, this can be generated after installing the environment:

```bash
pip freeze > requirements-lock.txt
```

For Node.js, keep the generated `package-lock.json` together with `package.json`.

## Configuration

The main paths and runtime settings are defined in `config.py`.

Important settings include:

* `DATA_DIR` — folder containing the SQLite databases.
* `DB_PATH` — path to the main project-level database.
* `FILES_DB_PATH` — path to the file-level metrics database.
* `REPOS_DIR` — folder where repositories are cloned temporarily.
* `REPO_TIMEOUT` — timeout for Git operations.
* `TIMEOUT_METRICS` — timeout for metric extraction.
* `COMMIT_EVERY` — number of processed repositories between database commits.
* `MAX_WORKERS` — number of parallel workers.

By default, repositories are cloned into:

```text
C:\repos
```

The data directory and repository directory are created automatically if they do not already exist.

## Input database

The pipeline expects a SQLite database at the path configured as `DB_PATH`.

The main input table is:

```text
projects
```

At minimum, the input table should contain:

| Column        | Description                                  |
| ------------- | -------------------------------------------- |
| `project_uid` | Unique identifier for the project.           |
| `github_link` | GitHub repository URL.                       |
| date column   | Date used to select the repository snapshot. |

The pipeline checks for several possible date column names, including:

* `end_date`
* `End_Date`
* `End_date`
* `date`
* `hackathon_date`

The date should represent the hackathon end date or the analysis snapshot date. It is used to find the latest commit at or before that date.

The pipeline normalizes GitHub links before cloning. For example:

```text
github.com/owner/repo
```

is normalized to:

```text
https://github.com/owner/repo.git
```

## Output databases

The pipeline writes two SQLite databases.

### 1. Project-level database

The main project-level output is stored in the database configured by:

```python
DB_PATH
```

The main table is:

```text
projects
```

This table contains one row per project. The pipeline adds missing schema columns automatically when `ensure_db_schema()` is called.

Important metadata columns include:

| Column          | Description                                       |
| --------------- | ------------------------------------------------- |
| `project_uid`   | Unique project identifier.                        |
| `github_link`   | Repository URL.                                   |
| `repo_name`     | Repository name.                                  |
| `lang`          | Detected main language: `python` or `javascript`. |
| `analysis_date` | Date used for the core repository snapshot.       |
| `commit`        | Commit hash used for the core snapshot.           |
| `commit_date`   | Date of the selected core commit.                 |
| `status`        | Core extraction status.                           |
| `error`         | Error message if extraction failed.               |

Core metric columns include:

| Column                   | Description                                                 |
| ------------------------ | ----------------------------------------------------------- |
| `files_count`            | Number of analyzed files for the detected language.         |
| `readme_words`           | Number of words in the root README file.                    |
| `sloc_total`             | Total source lines of code.                                 |
| `lloc_total`             | Total logical lines of code.                                |
| `functions_total`        | Total number of functions.                                  |
| `cc_total`               | Total cyclomatic complexity.                                |
| `comment_words_total`    | Total comment word count.                                   |
| `fan_in_*`               | Summary statistics for file/module fan-in.                  |
| `fan_out_*`              | Summary statistics for file/module fan-out.                 |
| `external_deps_*`        | Static, declared, and union dependency counts.              |
| `parse_ok_files`         | Number of files parsed successfully.                        |
| `parse_fail_files`       | Number of files that failed parsing.                        |
| `parse_fail_ratio`       | Share of parsed files that failed.                          |
| `suspect_all_zero`       | Flag for repositories where all main code metrics are zero. |
| `lang_files_count`       | Number of files in the detected language.                   |
| `all_source_files_count` | Number of supported source files in the repository.         |
| `lang_file_ratio_all`    | Share of supported source files in the detected language.   |

### 2. File-level metrics database

File-level metrics are stored in the database configured by:

```python
FILES_DB_PATH
```

The main table is:

```text
file_metrics
```

This table stores one row per analyzed file.

Important columns include:

| Column                 | Description                                             |
| ---------------------- | ------------------------------------------------------- |
| `project_uid`          | Unique project identifier.                              |
| `variant`              | Metric variant, usually `core` or `elbow`.              |
| `lang`                 | Language of the analyzed file.                          |
| `rel_path`             | File path relative to the repository root.              |
| `sloc`                 | Source lines of code in the file.                       |
| `lloc`                 | Logical lines of code in the file.                      |
| `functions`            | Number of functions in the file.                        |
| `cc_total`             | Total cyclomatic complexity in the file.                |
| `cc_mean_per_function` | Mean complexity per function in the file.               |
| `comment_words`        | Comment word count in the file.                         |
| `fan_in`               | Number of internal modules/files importing this file.   |
| `fan_out`              | Number of internal modules/files imported by this file. |
| `parse_ok`             | Whether the file parsed successfully.                   |
| `parse_error`          | Parse error message, if applicable.                     |

## Core and elbow metrics

The pipeline supports two metric variants.

### Core metrics

Core metrics describe the repository at the main analysis snapshot. This is usually the latest commit at or before the hackathon end date.

Core metrics are stored in columns without a prefix, for example:

* `commit`
* `commit_date`
* `lang`
* `sloc_total`
* `functions_total`
* `cc_total`
* `parse_fail_ratio`

### Elbow metrics

Elbow metrics describe a short-term post-event snapshot. The pipeline searches for the latest commit strictly after the hackathon end date and within a configured elbow window.

By default, the elbow window is 11 days.

Elbow metrics are stored with the `elbow_` prefix, for example:

* `elbow_lang`
* `elbow_commit`
* `elbow_commit_date`
* `elbow_days`
* `elbow_status`
* `elbow_error`
* `elbow_sloc_total`
* `elbow_functions_total`
* `elbow_cc_total`
* `elbow_parse_fail_ratio`

The elbow metrics are optional. The default pipeline run can be configured to skip them.

## Running the pipeline

Run the default pipeline with:

```bash
python main.py
```

The default `main.py` setup runs the core metric extraction without elbow metrics.

To enable elbow extraction, set:

```python
extract_elbow_metrics=True
```

in the relevant `run_pipeline()` call.

Example:

```python
run_pipeline(
    df_projects,
    force=False,
    run_name="core_pipeline",
    resume_progress=True,
    print_metric_changes=True,
    print_unchanged=False,
    extract_elbow_metrics=True,
    elbow_days=11,
)
```

## Progress and resume behavior

The pipeline can store progress files so interrupted runs can be resumed.

Progress files are stored in:

```text
Data/rerun_progress/
```

When `resume_progress=True`, project IDs that were already completed in the same run are skipped.

This is useful for long runs, because cloning repositories and extracting metrics can take substantial time.

## Status and error handling

The pipeline stores status and error information in the database instead of stopping the full run when one repository fails.

Common core status values include:

| Status            | Meaning                                                      |
| ----------------- | ------------------------------------------------------------ |
| `ok`              | Core extraction completed successfully.                      |
| `no_valid_commit` | No suitable commit was found at or before the analysis date. |
| `error`           | Repository processing failed.                                |
| `error-permanent` | Failure should not be retried automatically.                 |

Common elbow status values include:

| Status            | Meaning                                  |
| ----------------- | ---------------------------------------- |
| `ok`              | Elbow extraction completed successfully. |
| `no_elbow_commit` | No commit was found in the elbow window. |
| `not_run`         | Elbow extraction was not attempted.      |
| `error`           | Elbow extraction failed.                 |

Errors may occur because of deleted repositories, private repositories, invalid GitHub links, Git timeouts, Git LFS issues, invalid Windows paths, syntax errors, or parser failures.

## File filtering

The pipeline excludes files and directories that are unlikely to represent project source code.

Common exclusions include:

* `.git`
* virtual environments such as `.venv`, `venv`, `env`
* dependency folders such as `node_modules`, `site-packages`, `vendor`
* generated or build folders such as `dist`, `build`, `.next`, `coverage`
* generated files such as `.min.js`, `.bundle.js`, `.generated.py`, `.d.ts`
* binary-looking files
* very large source files

This filtering is intended to avoid counting dependencies, build outputs, and generated code as project-authored source code.

## Python metric extraction

Python metrics are extracted using the Python `ast` and `tokenize` modules.

The Python extractor computes:

* source lines of code,
* logical lines of code,
* function and lambda counts,
* function lengths,
* cyclomatic complexity,
* comment word counts,
* static imports,
* declared dependencies from `requirements.txt` and `pyproject.toml`,
* internal fan-in and fan-out,
* parse success and parse failure counts.

Docstrings are counted as comments and excluded from SLOC.

## JavaScript/TypeScript metric extraction

JavaScript and TypeScript metrics are extracted using a Node.js analyzer.

The Python file `metrics_js.py` calls:

```text
js_analyzer.js
```

The Node analyzer uses:

```text
@typescript-eslint/typescript-estree
```

It parses JavaScript, JSX, TypeScript, and TSX files using several parser configurations. If parsing fails, lexical SLOC and comment counts are still retained where possible.

The JavaScript/TypeScript extractor computes:

* source lines of code,
* logical lines of code,
* function counts,
* function lengths,
* cyclomatic complexity,
* comment word counts,
* static imports and require calls,
* declared dependencies from `package.json`,
* internal fan-in and fan-out,
* parse success and parse failure counts.
