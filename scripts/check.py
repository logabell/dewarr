"""Bounded, serial workstation checks. Full suites are explicit; CI selects suites separately."""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web"
UI_SMOKE = [
    "settings-navigation.spec.ts",
    "quick-add-sources.spec.ts",
    "configuration-deletion.spec.ts",
    "request-ledger.spec.ts",
]


def process_tree_rss(groups):
    """Track descendant groups too: Chromium starts a separate process group (KiB)."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,rss="],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    rows = [tuple(map(int, line.split())) for line in result.stdout.splitlines()]
    descendants = {pid for pid, _, group, _ in rows if group in groups}
    while True:
        children = {pid for pid, parent, _, _ in rows if parent in descendants}
        if children <= descendants:
            break
        descendants |= children
    groups.update(group for pid, _, group, _ in rows if pid in descendants)
    return sum(rss for pid, _, _, rss in rows if pid in descendants)


def stop_group(process, groups):
    # The leader may already have exited while a browser/worker is still alive.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in groups.copy():
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                groups.discard(group)
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                process.poll()  # Reap the group leader as soon as it exits.
                for group in groups.copy():
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        groups.discard(group)
                if not groups:
                    break
                time.sleep(0.1)
    process.wait(timeout=5)


def run(command, cwd, deadline, memory_mb):
    if time.monotonic() >= deadline:
        print("Stopped: no time remains for the next check.", file=sys.stderr)
        return 124
    print("+ " + " ".join(command), flush=True)
    env = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    env.pop("PYTEST_ADDOPTS", None)  # A shell's -n auto must not bypass the local budget.
    process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
    groups = {process.pid}
    peak = 0
    started = time.monotonic()
    try:
        while process.poll() is None:
            peak = max(peak, process_tree_rss(groups))
            if peak > memory_mb * 1024:
                print(f"Stopped: process group exceeded {memory_mb} MiB RSS.", file=sys.stderr)
                return 137
            if time.monotonic() >= deadline:
                print("Stopped: test run exceeded its wall-clock budget.", file=sys.stderr)
                return 124
            time.sleep(0.5)
        return process.returncode
    finally:
        stop_group(process, groups)
        print(f"Elapsed {time.monotonic() - started:.1f}s; peak sampled RSS {peak / 1024:.0f} MiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=["backend", "unit", "web-unit", "ui", "e2e"])
    parser.add_argument("tests", nargs="*", help="Specific test files or pytest node IDs")
    parser.add_argument("--full", action="store_true", help="Explicitly select the full suite")
    parser.add_argument("--list", action="store_true", help="Collect only; no build or servers")
    parser.add_argument(
        "--skip-build", action="store_true", help="Use an already current web build"
    )
    parser.add_argument("--timeout", type=int, default=300, help="Total seconds, including build")
    parser.add_argument(
        "--memory-mb", type=int, default=2048, help="Sampled process-group RSS budget"
    )
    args = parser.parse_args()
    if args.timeout <= 0 or args.memory_mb <= 0:
        parser.error("Budgets must be positive")
    if args.full and args.tests:
        parser.error("Select --full or test paths, not both")
    if any(path.startswith("-") for path in args.tests):
        parser.error("Only test paths/node IDs are accepted")
    if args.suite == "backend" and not args.list and not os.environ.get("BOOK_TEST_DATABASE_URL"):
        parser.error("Set BOOK_TEST_DATABASE_URL to a disposable database ending in _test")
    if args.suite == "e2e" and not args.list and not os.environ.get("BOOK_E2E_DATABASE_URL"):
        parser.error("Set BOOK_E2E_DATABASE_URL to a disposable database ending in _browser_test")

    commands = []
    if args.suite == "web-unit":
        paths = args.tests or [
            str(path.relative_to(WEB)) for path in sorted((WEB / "src").rglob("*.test.ts"))
        ]
        if args.list:
            print("\n".join(paths))
            return 0
        commands.append(
            (
                [
                    "node",
                    "--test",
                    "--test-concurrency=1",
                    "--test-timeout=30000",
                    *paths,
                ],
                WEB,
            )
        )
    elif args.suite in {"backend", "unit"}:
        if args.suite == "unit" and not (args.tests or args.full):
            parser.error(
                "Choose unit test paths, or --full; prefer backend journeys for routine checks"
            )
        paths = args.tests or (["tests/unit", "tests/contracts"] if args.suite == "unit" else [])
        if args.full and args.suite == "backend":
            paths = ["tests"]
        command = [
            "uv",
            "run",
            "--no-sync",
            "pytest",
            *paths,
            "-q",
            "-n",
            "0",
            "--maxfail=1",
            "--durations=10",
        ]
        if args.list:
            command.append("--collect-only")
        commands.append((command, ROOT))
    else:
        if not args.list and not args.skip_build:
            commands.append((["npm", "run", "build"], WEB))
        command = ["node", "node_modules/@playwright/test/cli.js", "test", "--workers=1"]
        if args.suite == "ui":
            command += ["--config=playwright.ui.config.ts"]
            command += args.tests or ([] if args.full else UI_SMOKE)
        else:
            command += ["--config=playwright.config.ts"]
            command += args.tests or (
                [] if args.full else ["reader-book-detail.spec.ts", "library-discovery.spec.ts"]
            )
        if args.list:
            command.append("--list")
        commands.append((command, WEB))

    (ROOT / ".local").mkdir(exist_ok=True)
    with (ROOT / ".local/check.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another bounded check is already running in this checkout")
        # Lower priority is inherited by compiler, test runner, API, and browser.
        os.nice(10)
        deadline = time.monotonic() + args.timeout
        for command, cwd in commands:
            result = run(command, cwd, deadline, args.memory_mb)
            if result:
                return result
    return 0


def interrupted(signum, frame):
    # A second Ctrl-C must not interrupt cleanup and leave children running.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
