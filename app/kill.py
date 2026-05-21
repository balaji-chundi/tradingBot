"""Kill-switch CLI entrypoint.

Sends SIGUSR1 to the running uvicorn process so the orchestrator flips
its `kill_switch_active` flag. The flag causes:
  - new ORB signals to be blocked with reason="kill_switch"
  - any open position to be force-exited on its next tick (ignoring stop/
    target/time-stop) via a market exit order through PaperBroker

Usage:
    python -m app.kill            # send SIGUSR1
    python -m app.kill --pid 1234 # explicit pid (skip pidfile lookup)

Exit codes:
    0  signal delivered
    2  pidfile missing
    3  process not running
    4  permission denied
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

DEFAULT_PID_FILE = Path("/tmp/trading-app-uv.pid")


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading-app kill switch.")
    parser.add_argument(
        "--pid", type=int, default=None, help="Override pidfile lookup with this PID."
    )
    parser.add_argument(
        "--pid-file",
        type=Path,
        default=DEFAULT_PID_FILE,
        help="Path to the uvicorn pidfile (default: /tmp/trading-app-uv.pid).",
    )
    args = parser.parse_args()

    if args.pid is not None:
        pid = args.pid
    else:
        if not args.pid_file.exists():
            print(f"kill: pidfile not found at {args.pid_file}", file=sys.stderr)
            return 2
        try:
            pid = int(args.pid_file.read_text().strip())
        except ValueError:
            print(f"kill: pidfile {args.pid_file} contains non-integer content", file=sys.stderr)
            return 2

    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        print(f"kill: pid {pid} not running", file=sys.stderr)
        return 3
    except PermissionError:
        print(f"kill: not permitted to signal pid {pid}", file=sys.stderr)
        return 4

    print(
        f"Sent SIGUSR1 to pid={pid}. "
        "Orchestrator will block new signals and force-exit open positions on the next tick."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
