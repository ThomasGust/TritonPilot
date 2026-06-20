"""Tiny paramiko helper to run commands on the ROV Pi.

Usage:
    python tools/pi_ssh.py "ip addr; ip route"
    echo "<long script>" | python tools/pi_ssh.py -      # read command from stdin
Env overrides:
    PI_HOST (default tritonpi.local), PI_USER (triton), PI_PASS (triton)
    PI_SUDO=1   run the whole command under `sudo -S` (password fed automatically)
"""
import os
import sys
import paramiko

HOST = os.environ.get("PI_HOST", "tritonpi.local")
USER = os.environ.get("PI_USER", "triton")
PASS = os.environ.get("PI_PASS", "triton")
USE_SUDO = os.environ.get("PI_SUDO") == "1"


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    if len(sys.argv) < 2:
        print("need a command argument (or '-' to read stdin)", file=sys.stderr)
        sys.exit(2)
    cmd = sys.stdin.read() if sys.argv[1] == "-" else " ".join(sys.argv[1:])
    cmd = cmd.lstrip("﻿").lstrip()  # drop UTF-8 BOM PowerShell may prepend on a pipe

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PASS, timeout=15,
                   allow_agent=False, look_for_keys=False)

    if USE_SUDO:
        full = f"sudo -S -p '' bash -c {shell_quote(cmd)}"
        stdin, stdout, stderr = client.exec_command(full, timeout=120)
        stdin.write(PASS + "\n")
        stdin.flush()
    else:
        stdin, stdout, stderr = client.exec_command(cmd, timeout=120)

    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err.strip():
        sys.stderr.write("\n[stderr]\n" + err)
    client.close()
    sys.exit(rc)


if __name__ == "__main__":
    main()
