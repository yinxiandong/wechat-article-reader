#!/usr/bin/env python3
"""Read-only independent verification of public scoring profiles and numeric report."""
import argparse
import json
import sys
from pathlib import Path

import evolution_core as core
import review_workspace as rw


def check(root):
    report_path = root / 'assets/evolution-report.json'
    return core.validate_publication(
        rw.load(root / 'assets/scoring-rubric.json'),
        rw.load(root / 'assets/published-profiles.json'),
        rw.load(root / 'assets/eval-seed.json'),
        rw.load(report_path) if report_path.exists() else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        print(json.dumps(check(args.root), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('ERROR: public scoring report validation failed; publication blocked.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
