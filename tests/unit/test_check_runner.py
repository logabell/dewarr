"""Exercise resource limits with real subprocesses, never the application stack."""

import os
import signal
import subprocess
import sys
import time

import pytest

from scripts.check import ROOT, run


def alive(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("exit_early", [False, True], ids=["timeout", "failed-parent"])
def test_check_reaps_stubborn_children_even_after_the_parent_exits(tmp_path, exit_early):
    pid_file = tmp_path / "child.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"p=Path({str(pid_file)!r}); "
        "\nwhile not p.exists(): time.sleep(0.01)\n"
        + ("sys.exit(7)" if exit_early else "time.sleep(60)")
    )
    try:
        result = run([sys.executable, "-c", parent], tmp_path, time.monotonic() + 2, 128)
        assert result == (7 if exit_early else 124)
        assert pid_file.exists(), "The child must start before exercising cleanup"
        assert not alive(int(pid_file.read_text()))
    finally:
        if pid_file.exists() and alive(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), 9)


def test_check_stops_a_process_group_over_its_memory_budget(tmp_path):
    # A detached child (like Chromium) must count toward the same memory budget.
    child = "import time; data=bytearray(64*1024*1024); time.sleep(60)"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); time.sleep(60)"
    )
    result = run(
        [sys.executable, "-c", parent],
        tmp_path,
        time.monotonic() + 5,
        32,
    )
    assert result == 137


def test_repeated_interrupts_do_not_abandon_child_cleanup(tmp_path):
    pid_file = tmp_path / "interrupted.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import signal,sys,time; from scripts.check import run,interrupted; "
        "signal.signal(signal.SIGTERM, interrupted); "
        f"\ntry: run([sys.executable,'-c',{child!r}],{str(tmp_path)!r},time.monotonic()+10,128)"
        "\nexcept KeyboardInterrupt: sys.exit(130)"
    )
    process = subprocess.Popen([sys.executable, "-c", parent], cwd=ROOT)
    try:
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=6) == 130
        assert not alive(int(pid_file.read_text()))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if pid_file.exists() and alive(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), 9)
