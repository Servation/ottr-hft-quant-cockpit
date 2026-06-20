#!/usr/bin/env python3
"""
Single entry point to run all eval scripts and report a pass/fail summary.

Why this exists: the individual eval scripts use inconsistent conventions
(some sys.exit(1), some only print a checkmark/cross). This runner normalizes
them into one report with a real process exit code, so it can be wired into CI
or a pre-push hook.

It also solves the "wrong endpoint" footgun: the LLM-backed evals read
LLM_BASE_URL (which .env may point at a Docker-only host like
host.docker.internal). This runner probes the common local endpoints, picks the
one that actually answers, and forces every child eval to use it — so a local
run "just works" without editing .env.

Usage:
    python run_evals.py                 # run everything
    python run_evals.py --no-llm        # skip evals that need a live LLM backend
    python run_evals.py eval_trades.py  # run only the named eval(s)
    python run_evals.py --base-url http://localhost:1234/v1   # force an endpoint
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# (script path, needs a live LLM backend?)
EVALS = [
    ("eval_trades.py", False),
    ("tests/eval_scheduler.py", False),
    ("eval_real_llm.py", True),
    ("eval_injection.py", True),
    ("tests/eval_identity.py", True),
    ("tests/eval_zephyr.py", True),
    ("tests/eval_meeting_phases.py", True),
]

FAIL_MARKERS = ("❌", "EVALUATION FAILED", "Traceback (most recent call last)",
                "AssertionError", "Evaluation failed!", "Connection error")
PASS_MARKERS = ("✅", "PASSED", "Completed Successfully")


def _probe(base: str) -> bool:
    url = base.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def find_reachable_base_url(explicit: str | None) -> str | None:
    """Return the first endpoint that answers, or None if none do."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.getenv("LLM_BASE_URL"):
        candidates.append(os.environ["LLM_BASE_URL"])
    candidates += [
        "http://localhost:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://host.docker.internal:1234/v1",
    ]
    seen = set()
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        if _probe(base):
            return base
    return None


def classify(returncode: int, output: str) -> str:
    if any(m in output for m in FAIL_MARKERS) or returncode != 0:
        return "FAIL"
    if any(m in output for m in PASS_MARKERS):
        return "PASS"
    return "UNKNOWN"


def run_one(script: str, timeout: int, base_url: str | None) -> tuple[str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if base_url:
        # Force the verified endpoint. load_dotenv(override=False) in some evals
        # will NOT clobber this, so it wins over .env.
        env["LLM_BASE_URL"] = base_url
    proc = subprocess.run(
        [sys.executable, script],
        cwd=str(ROOT),
        capture_output=True,
        # Decode child output as UTF-8 regardless of the OS locale. The eval
        # scripts emit UTF-8 (checkmarks, emojis); on Windows the default
        # locale decoder (cp1252) crashes on those bytes. errors="replace"
        # keeps a stray byte from taking down the whole run.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return classify(proc.returncode, output), output


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="Run only these eval script(s).")
    ap.add_argument("--no-llm", action="store_true", help="Skip LLM-backed evals.")
    ap.add_argument("--base-url", default=None, help="Force this LLM endpoint.")
    ap.add_argument("--timeout", type=int, default=600, help="Per-eval timeout (s).")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print full output.")
    args = ap.parse_args()

    selected = [(s, n) for s, n in EVALS if not args.only or s in args.only]

    base_url = None if args.no_llm else find_reachable_base_url(args.base_url)
    have_llm = base_url is not None
    if have_llm:
        print(f"LLM backend: REACHABLE at {base_url} (forced for all evals)")
    else:
        print("LLM backend: NOT reachable (LLM-backed evals will be skipped)")
    print("=" * 64)

    results = []
    for script, needs_llm in selected:
        if needs_llm and not have_llm:
            print(f"SKIP   {script}  (needs LLM backend)")
            results.append((script, "SKIP"))
            continue
        try:
            status, output = run_one(script, args.timeout, base_url)
        except subprocess.TimeoutExpired:
            status, output = "TIMEOUT", "(timed out)"
        print(f"{status:6} {script}")
        if args.verbose or status in ("FAIL", "UNKNOWN", "TIMEOUT"):
            print("\n".join("       | " + ln for ln in output.strip().splitlines()[-25:]))
        results.append((script, status))

    print("=" * 64)
    passed = sum(1 for _, s in results if s == "PASS")
    failed = [s for s, st in results if st in ("FAIL", "TIMEOUT", "UNKNOWN")]
    skipped = sum(1 for _, s in results if s == "SKIP")
    print(f"Summary: {passed} passed, {len(failed)} failed, {skipped} skipped")
    if failed:
        print("Failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
