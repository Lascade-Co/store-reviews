#!/usr/bin/env python3
"""Run every test in this folder with one command (no PYTHONPATH needed):

    python3 tests/run_all.py

Exits 0 when all tests pass, 1 when anything fails.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The code under test imports modules as "common.*" / "providers.*",
# so scripts/ must be importable exactly like the workflow runs it.
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _print_summary(result: unittest.TestResult) -> None:
    """Print a compact pass/fail scoreboard so nobody has to read the full log."""
    failed = len(result.failures)
    errored = len(result.errors)
    skipped = len(result.skipped)
    passed = result.testsRun - failed - errored - skipped

    print()
    print("=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    print(f"  Total   : {result.testsRun}")
    print(f"  Passed  : {passed}")
    print(f"  Failed  : {failed}")
    print(f"  Errors  : {errored}")
    if skipped:
        print(f"  Skipped : {skipped}")

    broken = [(test, "FAILED") for test, _ in result.failures]
    broken += [(test, "ERROR") for test, _ in result.errors]
    if broken:
        print()
        print("  Broken tests:")
        for test, kind in broken:
            print(f"    ✗ [{kind}] {test.id()}")
        print()
        print("  ✗ NOT SAFE TO PUSH — fix the tests above first.")
    else:
        print()
        print("  ✓ ALL TESTS PASSED — safe to push.")
    print("=" * 70)


def main() -> int:
    suite = unittest.defaultTestLoader.discover(start_dir=str(REPO_ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    _print_summary(result)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
