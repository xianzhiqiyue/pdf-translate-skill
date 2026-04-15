#!/usr/bin/env python3
"""Install pdf-translate runtime dependencies with mirror and timeout defaults."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from typing import Sequence

DEFAULT_MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.mirrors.ustc.edu.cn/simple",
    "https://pypi.doubanio.com/simple",
]
RUNTIME_PACKAGES = ["pymupdf"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install pdf-translate runtime dependencies with long pip timeouts and China-friendly mirrors."
    )
    parser.add_argument("--python", default=sys.executable, help="Python executable to install into (default: current Python)")
    parser.add_argument("--timeout", type=int, default=300, help="pip network timeout seconds (default: 300)")
    parser.add_argument("--retries", type=int, default=8, help="pip network retries per mirror (default: 8)")
    parser.add_argument(
        "--mirror",
        action="append",
        help="Package index mirror URL. Can be repeated. Defaults to common China mirrors.",
    )
    parser.add_argument(
        "--package",
        action="append",
        help="Package to install. Can be repeated. Defaults to runtime dependencies.",
    )
    parser.add_argument("--upgrade", action="store_true", help="Pass --upgrade to pip")
    parser.add_argument("--dry-run", action="store_true", help="Print pip commands without executing them")
    parser.add_argument(
        "--no-mirror",
        action="store_true",
        help="Use pip's configured/default index instead of the built-in China mirror list.",
    )
    return parser.parse_args()


def pip_command(args: argparse.Namespace, packages: Sequence[str], mirror: str | None) -> list[str]:
    command = [
        args.python,
        "-m",
        "pip",
        "install",
        "--timeout",
        str(args.timeout),
        "--retries",
        str(args.retries),
        "--disable-pip-version-check",
    ]
    if args.upgrade:
        command.append("--upgrade")
    if mirror:
        command.extend(["-i", mirror, "--trusted-host", mirror.split("//", 1)[-1].split("/", 1)[0]])
    command.extend(packages)
    return command


def run(command: list[str]) -> int:
    print("+ " + shlex.join(command), flush=True)
    return subprocess.run(command).returncode


def main() -> int:
    args = parse_args()
    packages = args.package or RUNTIME_PACKAGES
    mirrors: list[str | None]
    if args.no_mirror:
        mirrors = [None]
    else:
        mirrors = args.mirror or DEFAULT_MIRRORS

    last_code = 1
    for mirror in mirrors:
        label = mirror or "pip default index"
        print(f"Installing {', '.join(packages)} via {label} (timeout={args.timeout}s, retries={args.retries})", flush=True)
        command = pip_command(args, packages, mirror)
        if args.dry_run:
            print("+ " + shlex.join(command), flush=True)
            last_code = 0
        else:
            last_code = run(command)
        if last_code == 0:
            print("Dependencies installed successfully." if not args.dry_run else "Dry run complete.", flush=True)
            return 0
        print(f"Install failed via {label}; trying next index if available.", file=sys.stderr, flush=True)

    print("Dependency installation failed for all configured indexes.", file=sys.stderr)
    print(
        "Try: python3 -m pip install --timeout 300 --retries 8 -i https://pypi.tuna.tsinghua.edu.cn/simple pymupdf",
        file=sys.stderr,
    )
    return last_code


if __name__ == "__main__":
    raise SystemExit(main())
