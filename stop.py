#!/usr/bin/env python3
"""Para o servidor DPU-script-SIS (le PID file + limpa porta)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
import contextlib

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".server.pid"
PORT = 8001


def _killp_windows(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def _killp_posix(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def _sweep_porta_windows(port: int) -> None:
    """Mata tudo que esta LISTEN na porta (netstat + taskkill)."""
    result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid > 4 and pid not in seen:
            _killp_windows(pid)
            seen.add(pid)
            print(f"[stop] matou processo na porta {port}: PID {pid}")


def _sweep_porta_posix(port: int) -> None:
    """Mata tudo que esta escutando na porta — usa lsof (Linux/Mac). Se lsof
    nao estiver instalado, avisa silenciosamente e segue — o PID file ja
    derrubou o processo nos casos comuns."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print(f"[stop] (aviso) lsof nao encontrado — pulando sweep da porta {port}")
        return
    seen: set[int] = set()
    for linha in result.stdout.splitlines():
        try:
            pid = int(linha.strip())
        except ValueError:
            continue
        if pid in seen or pid == os.getpid():
            continue
        _killp_posix(pid)
        seen.add(pid)
        print(f"[stop] matou processo na porta {port}: PID {pid}")


def main() -> int:
    # PID file
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if os.name == "nt":
                _killp_windows(pid)
            else:
                _killp_posix(pid)
            print(f"[stop] matou PID {pid} (do PID file)")
        except ValueError:
            pass
        PID_FILE.unlink(missing_ok=True)

    # Garante que a porta ta livre
    if os.name == "nt":
        _sweep_porta_windows(PORT)
    else:
        _sweep_porta_posix(PORT)

    print("[stop] servidor parado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
