#!/usr/bin/env python3
"""Smoke test: the plugin must never crash, even with no credentials available.

A SwiftBar plugin that raises renders as a broken menu bar for every user, so the
contract checked here is deliberately about resilience rather than correctness:

  - exit status 0
  - no traceback on stderr
  - a non-empty menu-bar line (SwiftBar reads line 1)
  - at least one '---' separator, so the dropdown is well formed

Each case runs with HOME pointed at a throwaway directory, so the real cache in
~/.cache is never read or written and the plugin always takes a cold-start path.
On a CI runner there is no Claude token in the keychain, which exercises the
credential-failure branch — the one most likely to regress unnoticed, since it
almost never fires on the maintainer's own machine.
"""

import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "claude-usage.1m.py")


def check(label, args):
    """Run the plugin once in an isolated HOME; return a list of problems."""
    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(
            [sys.executable, PLUGIN, *args],
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(os.environ, HOME=home),
        )

    problems = []
    if proc.returncode != 0:
        problems.append(f"exit status {proc.returncode} (want 0)")
    if "Traceback" in proc.stderr:
        problems.append("traceback on stderr")

    lines = proc.stdout.splitlines()
    if not lines or not lines[0].strip():
        problems.append("empty menu-bar line (SwiftBar renders line 1)")
    if "---" not in lines:
        problems.append("no '---' separator in output")

    print(f"--- {label}: {'FAIL' if problems else 'ok'}")
    for p in problems:
        print(f"      {p}")
    if problems:
        print(f"      stdout:\n{proc.stdout or '(empty)'}")
        print(f"      stderr:\n{proc.stderr or '(empty)'}")
    return problems


def main():
    failures = 0
    failures += len(check("no credentials, cold cache", []))
    failures += len(check("no credentials, --force", ["--force"]))

    if failures:
        print(f"\nsmoke test FAILED ({failures} problem(s))")
        return 1
    print("\nsmoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
