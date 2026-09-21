#!/usr/bin/env python3
"""Prove this runs on the Python version claimed, not just the one you have.

Compiles every file, imports every module -- the tools included, which the
test suites never touch -- runs both suites, and checks each CLI answers
--help. Run it under whichever interpreter you want to support:

    python3 tools/check_python.py

Across versions, without installing any of them:

    for v in 3.8 3.9 3.10 3.11 3.12 3.13; do
        podman run --rm -v "$PWD":/src:z -w /src \
            docker.io/library/python:$v-slim python tools/check_python.py
    done

Use :z and run them one at a time. Two concurrent :Z mounts relabel the same
directory and the containers lock each other out with "Permission denied",
which looks convincingly like a real failure.

Two genuine incompatibilities turned up this way, neither visible by reading:
socket.timeout only became an alias of TimeoutError in 3.10, so `except
TimeoutError` silently missed it and a slow device read as offline; and
Path.is_relative_to arrived in 3.9.
"""
import py_compile, subprocess, sys, importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import os
os.chdir(ROOT)

version = "%d.%d" % sys.version_info[:2]
print(f"=== Python {version} ===")

failed = []

# 1. Every file compiles.
files = sorted(p for p in Path(".").rglob("*.py") if ".git" not in p.parts)
for f in files:
    try:
        py_compile.compile(str(f), doraise=True, cfile="/tmp/pyc.tmp")
    except py_compile.PyCompileError as exc:
        failed.append(f"compile {f}: {exc}")
print(f"compiled {len(files)} files: {'OK' if not failed else 'FAILED'}")

# 2. Every module imports (tools included -- the suites never import those).
sys.path.insert(0, ".")
sys.path.insert(0, "streamer")
sys.path.insert(0, "tools")
sys.path.insert(0, "streamer/tools")
modules = [
    "eclermanager.veo", "eclermanager.config", "eclermanager.discovery",
    "eclermanager.poller", "eclermanager.server", "eclermanager.auth",
    "eclerstreamer.config", "eclerstreamer.control", "eclerstreamer.server",
    "eclerstreamer.auth",
    "probe", "scan", "mock_veo", "setpassword", "sniffmac", "macdiff",
    "arpscan", "forward", "setip", "setname",
    "teststream",
]
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(f"import {name}: {type(exc).__name__}: {exc}")
print(f"imported {len(modules)} modules: "
      f"{'OK' if not any('import' in f for f in failed) else 'FAILED'}")

# 3. Both suites.
for suite in ("tests", "streamer/tests"):
    done = subprocess.run([sys.executable, "-m", "unittest", "discover",
                           "-s", suite, "-q"], capture_output=True, text=True)
    tail = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "?"
    print(f"{suite}: {tail}")
    if done.returncode != 0:
        failed.append(f"{suite}: {tail}")

# 4. The CLIs answer --help without exploding.
for cli in (["run.py", "--help"], ["streamer/run.py", "--help"],
            ["streamer/stream.py", "--help"],
            ["streamer/tools/teststream.py", "--help"]):
    done = subprocess.run([sys.executable] + cli, capture_output=True, text=True)
    if done.returncode != 0:
        failed.append(f"{' '.join(cli)}: {done.stderr.strip()[-200:]}")
print(f"CLIs: {'OK' if not any('--help' in f for f in failed) else 'FAILED'}")

print()
if failed:
    print(f"!! {len(failed)} PROBLEM(S) on {version}")
    for line in failed:
        print("   ", line)
    sys.exit(1)
print(f"ALL CLEAR on {version}")
