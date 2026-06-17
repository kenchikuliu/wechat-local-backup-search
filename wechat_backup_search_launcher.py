#!/usr/bin/env python3
"""Standalone launcher for the backup/search product build."""

from __future__ import annotations

import os
import runpy
import sys
import traceback


SCRIPT_ENTRYPOINTS = {
    "backup_search_desktop.py",
    "backup_search_app.py",
    "main.py",
    "decrypt_3x_db.py",
    "decrypt_db.py",
    "export_3x_chats.py",
    "export_all_chats.py",
    "find_all_keys.py",
    "find_3x_key_windows.py",
    "find_all_keys_windows.py",
    "wechat_process_check.py",
}


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _script_dir():
    return getattr(sys, "_MEIPASS", _app_base_dir())


def _prepare_runtime():
    base_dir = _app_base_dir()
    os.environ["WECHAT_DECRYPT_APP_DIR"] = base_dir
    os.chdir(base_dir)


def _configure_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _resolve(argv):
    if not argv:
        return "backup_search_desktop.py", []
    cmd = argv[0]
    if cmd in {"backup-search", "backup", "search-app", "desktop"}:
        return "backup_search_desktop.py", argv[1:]
    if cmd == "web":
        return "backup_search_app.py", argv[1:]
    if cmd in {"decrypt", "export", "status", "-s"}:
        return "main.py", argv
    if cmd == "export-all":
        return "export_all_chats.py", argv[1:]
    if cmd == "check-wechat":
        return "wechat_process_check.py", argv[1:]
    if cmd in SCRIPT_ENTRYPOINTS:
        return cmd, argv[1:]
    if cmd in {"help", "-h", "--help"}:
        return None, None
    if cmd == "--debug-launch":
        return "backup_search_desktop.py", []
    return None, None


def print_usage():
    print(
        "WeChatBackupSearch.exe 用法:\n"
        "\n"
        "  WeChatBackupSearch.exe                  启动桌面备份搜索软件\n"
        "  WeChatBackupSearch.exe backup-search    启动桌面备份搜索软件\n"
        "  WeChatBackupSearch.exe web              启动备用浏览器界面\n"
        "  WeChatBackupSearch.exe status           查看状态\n"
        "  WeChatBackupSearch.exe check-wechat     只检测微信进程\n"
        "  WeChatBackupSearch.exe decrypt          提取密钥并解密数据库\n"
        "  WeChatBackupSearch.exe export-all ...   直接调用导出脚本\n"
    )


def run_script(script, script_args):
    _prepare_runtime()
    script_path = os.path.join(_script_dir(), script)
    if not os.path.exists(script_path):
        script_path = os.path.join(_app_base_dir(), script)
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"脚本不存在: {script_path}")

    old_argv = sys.argv[:]
    try:
        sys.argv = [script] + list(script_args)
        try:
            runpy.run_path(script_path, run_name="__main__")
        except SystemExit as exc:
            if exc.code is None:
                return 0
            if isinstance(exc.code, int):
                return exc.code
            print(exc.code, file=sys.stderr)
            return 1
        return 0
    finally:
        sys.argv = old_argv


def main():
    _configure_stdio()
    argv = sys.argv[1:]
    debug = "--debug-launch" in argv
    script, script_args = _resolve(argv)
    if script is None:
        print_usage()
        sys.exit(0)
    try:
        sys.exit(run_script(script, script_args))
    except Exception:
        if debug:
            traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
