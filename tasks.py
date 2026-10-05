#!/usr/bin/env python3
"""
Project commands. Works the same on Windows, macOS and Linux; no make or bash needed.

    python tasks.py install    install the Python packages
    python tasks.py dev        start the app on http://127.0.0.1:8000
    python tasks.py test       run all three test suites
    python tasks.py test-py    server tests
    python tasks.py test-js    encryption tests (needs Node.js 20+)
    python tasks.py e2e        end-to-end test against a throwaway server (needs Node.js 20+)
    python tasks.py check      Django checks and missing-migration check

On Windows, use `py tasks.py ...` if `python` isn't recognised.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COORDINATOR = ROOT / "coordinator"
WEB = ROOT / "web"
PYTHON = sys.executable


def run(command, cwd=ROOT, env=None):
    print("> " + " ".join(str(part) for part in command), flush=True)
    result = subprocess.run([str(part) for part in command], cwd=cwd, env=env)
    if result.returncode != 0:
        sys.exit(result.returncode)


def django_env(**extra):
    env = os.environ.copy()
    env.setdefault("DJANGO_DEBUG", "1")
    env.update({key: str(value) for key, value in extra.items()})
    return env


def require_node():
    node = shutil.which("node")
    if not node:
        sys.exit("This step needs Node.js 20 or newer. Install it from https://nodejs.org, then open a new terminal.")
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    if int(version.lstrip("v").split(".")[0]) < 20:
        sys.exit(f"This step needs Node.js 20 or newer; found {version}.")
    return node


def install():
    run([PYTHON, "-m", "pip", "install", "-r", COORDINATOR / "requirements.txt"])


def dev():
    env = django_env()
    run([PYTHON, "manage.py", "migrate"], cwd=COORDINATOR, env=env)
    print("\nOpen http://127.0.0.1:8000 in your browser. Press Ctrl+C to stop.\n", flush=True)
    try:
        subprocess.run([PYTHON, "manage.py", "runserver", "127.0.0.1:8000"], cwd=COORDINATOR, env=env)
    except KeyboardInterrupt:
        pass


def test_py():
    run([PYTHON, "manage.py", "test"], cwd=COORDINATOR, env=django_env(LOG_LEVEL="ERROR"))


def test_js():
    node = require_node()
    files = sorted(glob.glob(str(WEB / "test" / "*.test.js")))
    run([node, "--test", *files], cwd=WEB)


def check():
    env = django_env()
    run([PYTHON, "manage.py", "check"], cwd=COORDINATOR, env=env)
    run([PYTHON, "manage.py", "makemigrations", "--check", "--dry-run"], cwd=COORDINATOR, env=env)


def e2e(port=8765):
    """Start a throwaway server (fresh database and storage folder), test it, clean up."""
    node = require_node()
    work = Path(tempfile.mkdtemp(prefix="dstore-e2e-"))
    staging = work / "staging"
    env = django_env(
        DJANGO_SECRET_KEY=f"e2e-only-{time.time()}",
        DATABASE_URL=f"sqlite:///{(work / 'db.sqlite3').as_posix()}",
        STAGING_DIR=staging,
        AUTH_THROTTLE_RATE="300/minute",
        AUTH_USER_THROTTLE_RATE="100/minute",
        LOG_LEVEL="WARNING",
    )
    log_path = work / "server.log"
    server = None
    log = open(log_path, "w")
    try:
        run([PYTHON, "manage.py", "migrate", "--noinput", "-v", "0"], cwd=COORDINATOR, env=env)
        server = subprocess.Popen(
            [PYTHON, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
            cwd=COORDINATOR,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        base_url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(f"{base_url}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("The test server didn't start.")

        result = subprocess.run(
            [node, ROOT / "scripts" / "e2e.mjs"],
            cwd=ROOT,
            env={**env, "BASE_URL": base_url, "STAGING_DIR": str(staging)},
        )
        if result.returncode != 0:
            log.flush()
            print("\n--- server log ---")
            print("".join(log_path.read_text(errors="replace").splitlines(keepends=True)[-50:]))
            sys.exit(result.returncode)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


def test():
    test_py()
    test_js()
    e2e()


COMMANDS = {
    "install": install,
    "dev": dev,
    "test": test,
    "test-py": test_py,
    "test-js": test_js,
    "e2e": e2e,
    "check": check,
}


def main():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is needed.")
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
