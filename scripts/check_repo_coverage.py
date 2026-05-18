#!/usr/bin/env python3
"""Validate combined backend and frontend line coverage."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def parse_backend_coverage(path: Path) -> tuple[int, int]:
    text = path.read_text()
    covered_match = re.search(r'lines-covered="(\d+)"', text)
    total_match = re.search(r'lines-valid="(\d+)"', text)
    if covered_match is None or total_match is None:
        raise ValueError(f"Could not parse backend coverage totals from {path}")
    return int(covered_match.group(1)), int(total_match.group(1))


def parse_frontend_coverage(path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text())
    total = data["total"]["lines"]
    return int(total["covered"]), int(total["total"])


def format_percent(covered: int, total: int) -> float:
    return (covered / total * 100.0) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True, help="Path to backend coverage.xml")
    parser.add_argument(
        "--frontend",
        type=Path,
        required=True,
        help="Path to frontend coverage-summary.json",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        help="Minimum combined line coverage percentage",
    )
    args = parser.parse_args()

    backend_covered, backend_total = parse_backend_coverage(args.backend)
    frontend_covered, frontend_total = parse_frontend_coverage(args.frontend)

    total_covered = backend_covered + frontend_covered
    total_lines = backend_total + frontend_total

    backend_pct = format_percent(backend_covered, backend_total)
    frontend_pct = format_percent(frontend_covered, frontend_total)
    combined_pct = format_percent(total_covered, total_lines)

    print(
        f"Backend line coverage: {backend_covered}/{backend_total} ({backend_pct:.2f}%)",
    )
    print(
        f"Frontend line coverage: {frontend_covered}/{frontend_total} ({frontend_pct:.2f}%)",
    )
    print(
        f"Combined repo line coverage: {total_covered}/{total_lines} ({combined_pct:.2f}%)",
    )

    if combined_pct < args.threshold:
        print(
            f"Combined repo coverage {combined_pct:.2f}% is below the required {args.threshold:.2f}%.",
            file=sys.stderr,
        )
        return 1

    print(f"Combined repo coverage meets the {args.threshold:.2f}% threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
