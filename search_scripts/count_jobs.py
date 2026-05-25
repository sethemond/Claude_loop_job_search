#!/usr/bin/env python3
"""
count_jobs.py — Return the number of job entries in jobs.json.

Public API:
    from search_scripts.count_jobs import get_job_count
    n = get_job_count()                    # uses default jobs.json in project root
    n = get_job_count("path/to/jobs.json") # custom path

CLI:
    python search_scripts/count_jobs.py [--path PATH]
    → prints a single integer to stdout
    → exits 0 on success, 1 on error

Handles both a top-level JSON array and {"jobs": [...]} object format.
Returns 0 if the file is missing or cannot be parsed (never raises).
"""

import argparse
import json
import sys
from pathlib import Path

_DEFAULT_JOBS_PATH = Path(__file__).resolve().parent.parent / "jobs.json"


def get_job_count(jobs_path: "str | Path | None" = None) -> int:
    """Return the number of job entries in jobs.json.

    Args:
        jobs_path: Path to jobs.json. Defaults to jobs.json in project root.

    Returns:
        Integer count of job entries, or 0 on any read/parse failure.
    """
    path = Path(jobs_path) if jobs_path is not None else _DEFAULT_JOBS_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            jobs = data.get("jobs", [])
            if isinstance(jobs, list):
                return len(jobs)
    except Exception:
        pass
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print the number of job entries in jobs.json.",
    )
    parser.add_argument(
        "--path",
        default=None,
        help=f"Path to jobs.json (default: {_DEFAULT_JOBS_PATH})",
    )
    args = parser.parse_args()

    path = Path(args.path) if args.path else _DEFAULT_JOBS_PATH
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    count = get_job_count(path)
    print(count)
    sys.exit(0)


if __name__ == "__main__":
    main()
