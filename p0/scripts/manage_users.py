#!/usr/bin/env python3
"""Operate the P0 auth store without exposing passwords in argv/history.

Usage (run on the server, from /opt/cq-ai-p0):

    .venv/bin/python scripts/manage_users.py list
    .venv/bin/python scripts/manage_users.py add <user> <role>
    .venv/bin/python scripts/manage_users.py reset <user>

Passwords are read from stdin (getpass), never from a command-line argument.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "list":
        for user in auth.load_users():
            print(f"{user.get('username')}\t{user.get('role')}\t{user.get('display_name', '')}")
        return 0
    if command == "add":
        if len(argv) != 4:
            print("usage: manage_users.py add <user> <admin|reviewer|viewer>", file=sys.stderr)
            return 2
        username, role = argv[2], argv[3]
        password = getpass.getpass("新密码（至少 8 位）: ")
        confirm = getpass.getpass("再输入一次: ")
        if password != confirm:
            print("两次密码不一致", file=sys.stderr)
            return 1
        try:
            auth.create_user(username, password, role=role)
        except ValueError as exc:
            print(f"创建失败：{exc}", file=sys.stderr)
            return 1
        print(f"已创建 {username}（{role}）")
        return 0
    if command == "reset":
        if len(argv) != 3:
            print("usage: manage_users.py reset <user>", file=sys.stderr)
            return 2
        username = argv[2]
        password = getpass.getpass("新密码（至少 8 位）: ")
        confirm = getpass.getpass("再输入一次: ")
        if password != confirm:
            print("两次密码不一致", file=sys.stderr)
            return 1
        try:
            auth.update_password(username, password)
        except (KeyError, ValueError) as exc:
            print(f"重置失败：{exc}", file=sys.stderr)
            return 1
        print(f"已重置 {username}")
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
