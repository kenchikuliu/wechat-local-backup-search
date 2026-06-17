#!/usr/bin/env python3
"""Local WeChat backup and search app.

This module wraps the existing decrypt/export scripts into a localhost-only
browser app:

1. Incrementally decrypt WeChat databases.
2. Incrementally export chats to JSON.
3. Rebuild a local SQLite search index.
4. Serve a small UI for backup status and fast search.

All data stays on the local machine. The HTTP server binds to 127.0.0.1 by
default and rejects non-loopback clients as a defense-in-depth measure.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
import atexit
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path

from backup_search_knowledge import (
    ChatAccumulator,
    chat_identity,
    compact_text,
    decode_json_list,
    related_query_terms,
    short_snippet,
)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get("WECHAT_DECRYPT_APP_DIR", SCRIPT_DIR)
HOST = os.environ.get("WECHAT_BACKUP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WECHAT_BACKUP_PORT", "5680"))
MIN_BACKUP_FREE_BYTES = 5 * 1024 * 1024 * 1024


def _candidate_backup_roots() -> list[str]:
    roots = [APP_DIR]
    if os.name == "nt":
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if os.path.isdir(root):
                roots.append(os.path.join(root, "WeChatBackupSearchData"))
    return roots


def _choose_default_data_dir() -> str:
    requested = os.environ.get("WECHAT_BACKUP_DATA_DIR")
    if requested:
        return os.path.abspath(requested)

    best_path = os.path.join(APP_DIR, "backup_search_data")
    best_free = -1
    for candidate in _candidate_backup_roots():
        try:
            target = candidate if os.path.isdir(candidate) else os.path.dirname(candidate) or candidate
            usage = shutil.disk_usage(target)
        except OSError:
            continue
        if candidate.startswith(APP_DIR) and usage.free >= MIN_BACKUP_FREE_BYTES:
            return os.path.join(APP_DIR, "backup_search_data")
        if usage.free > best_free:
            best_free = usage.free
            best_path = candidate
    if os.path.basename(best_path).lower() != "backup_search_data":
        best_path = os.path.join(best_path, "backup_search_data")
    return os.path.abspath(best_path)


DATA_DIR = _choose_default_data_dir()
EXPORT_DIR = os.path.join(DATA_DIR, "exported_chats")
INDEX_DB = os.path.join(DATA_DIR, "search_index.sqlite3")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "backup.log")
TASK_LOCK_FILE = os.path.join(DATA_DIR, "task.lock")
BACKGROUND_STATE_FILE = os.path.join(DATA_DIR, "background_agent_state.json")
KNOWLEDGE_NEIGHBOR_LIMIT = 6

MIN_INTERVAL_MINUTES = 1
DEFAULT_SETTINGS = {
    "auto_backup_enabled": False,
    "backup_interval_minutes": 30,
    "with_transcriptions": False,
    "last_backup_at": "",
    "last_backup_ok": None,
    "last_backup_summary": "",
    "last_indexed_at": "",
    "last_error": "",
}


def _wechat_data_style() -> str:
    return "wx_3x" if _find_3x_msg_root() is not None else "wx_4x"


def _find_3x_msg_root() -> Path | None:
    root = Path.home() / "Documents" / "WeChat Files"
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for micro in root.glob("wxid_*/Msg/MicroMsg.db"):
        try:
            candidates.append(micro.parent)
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[0]

state_lock = threading.RLock()
job_lock = threading.Lock()
settings_lock = threading.RLock()
job_state = {
    "running": False,
    "kind": "",
    "started_at": "",
    "finished_at": "",
    "step": "",
    "ok": None,
    "message": "",
}
scheduler_stop = threading.Event()
task_lock_handle = None


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)


def _append_log(line: str) -> None:
    _ensure_dirs()
    stamp = _now_text()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line.rstrip()}\n")


def _write_json_file(path: str, payload: dict) -> None:
    _ensure_dirs()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _read_json_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_background_state(**updates) -> dict:
    state = _read_json_file(BACKGROUND_STATE_FILE)
    state.update(updates)
    state.setdefault("updated_at", _now_text())
    state["updated_at"] = _now_text()
    _write_json_file(BACKGROUND_STATE_FILE, state)
    return state


def _background_state() -> dict:
    state = _read_json_file(BACKGROUND_STATE_FILE)
    pid = int(state.get("pid") or 0)
    running = False
    if pid > 0:
        try:
            if os.name == "nt":
                running = True if subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                ).stdout.find(str(pid)) >= 0 else False
            else:
                os.kill(pid, 0)
                running = True
        except Exception:
            running = False
    if pid > 0 and not running:
        state["running"] = False
        try:
            os.remove(BACKGROUND_STATE_FILE)
        except OSError:
            pass
        return {}
    state["running"] = running
    return state


def _clear_background_state() -> None:
    if os.path.exists(BACKGROUND_STATE_FILE):
        try:
            os.remove(BACKGROUND_STATE_FILE)
        except OSError:
            pass


def _release_task_lock() -> None:
    global task_lock_handle
    handle = task_lock_handle
    task_lock_handle = None
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass


def _acquire_task_lock() -> bool:
    global task_lock_handle
    if task_lock_handle is not None:
        return True
    _ensure_dirs()
    handle = open(TASK_LOCK_FILE, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    task_lock_handle = handle
    return True


atexit.register(_release_task_lock)


def _load_settings() -> dict:
    _ensure_dirs()
    if not os.path.exists(SETTINGS_FILE):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw if isinstance(raw, dict) else {})
    try:
        interval = int(settings.get("backup_interval_minutes", 30))
    except (TypeError, ValueError):
        interval = 30
    settings["backup_interval_minutes"] = max(MIN_INTERVAL_MINUTES, interval)
    settings["auto_backup_enabled"] = bool(settings.get("auto_backup_enabled"))
    settings["with_transcriptions"] = bool(settings.get("with_transcriptions"))
    return settings


def _save_settings(settings: dict) -> None:
    _ensure_dirs()
    safe = dict(DEFAULT_SETTINGS)
    safe.update(settings)
    safe["backup_interval_minutes"] = max(
        MIN_INTERVAL_MINUTES, int(safe.get("backup_interval_minutes") or 30)
    )
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)


def _update_settings(**updates) -> dict:
    with settings_lock:
        settings = _load_settings()
        settings.update(updates)
        _save_settings(settings)
        return settings


def _set_job(**updates) -> None:
    with state_lock:
        job_state.update(updates)


def _script_command(script: str, *args: str) -> list[str]:
    # When packaged, sys.executable is the exe and accepts script-style
    # dispatch: WeChatBackupSearch.exe main.py decrypt.
    # When running from source, use the current interpreter and the local script.
    if getattr(sys, "frozen", False):
        return [sys.executable, script, *args]
    return [sys.executable, os.path.join(SCRIPT_DIR, script), *args]


def _three_x_key_file() -> str:
    return os.path.join(APP_DIR, "wechat_3x_key.json")


def _ensure_3x_key_file() -> str:
    key_file = _three_x_key_file()
    if os.path.exists(key_file):
        return key_file
    code, output = _run_command(_script_command("find_3x_key_windows.py"))
    if code != 0 or not os.path.exists(key_file):
        raise RuntimeError(_tail_error(output) or "提取 3.x 密钥失败")
    return key_file


def _backup_commands(settings: dict) -> tuple[list[str], list[str]]:
    if _wechat_data_style() == "wx_3x":
        msg_root = _find_3x_msg_root()
        if msg_root is None:
            raise RuntimeError("未找到 Windows WeChat 3.x 的 Msg 目录")
        key_file = _ensure_3x_key_file()
        decrypt_cmd = _script_command(
            "decrypt_3x_db.py",
            "-k",
            key_file,
            "-i",
            str(msg_root),
            "-o",
            os.path.join(DATA_DIR, "decrypted_3x"),
            "--incremental",
        )
        export_cmd = _script_command(
            "export_3x_chats.py",
            os.path.join(DATA_DIR, "decrypted_3x"),
            EXPORT_DIR,
            "--incremental",
        )
        if settings.get("with_transcriptions"):
            export_cmd.append("--with-transcriptions")
        return decrypt_cmd, export_cmd

    decrypt_cmd = _script_command("main.py", "decrypt", "--incremental")
    export_cmd = _script_command("export_all_chats.py", EXPORT_DIR, "--incremental")
    if settings.get("with_transcriptions"):
        export_cmd.append("--with-transcriptions")
    return decrypt_cmd, export_cmd


def _run_command(args: list[str], timeout: int | None = None) -> tuple[int, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("WECHAT_DECRYPT_APP_DIR", APP_DIR)
    display = " ".join(args)
    _append_log(f"$ {display}")
    proc = subprocess.run(
        args,
        cwd=APP_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=timeout,
    )
    output = proc.stdout or ""
    if output.strip():
        tail = "\n".join(output.splitlines()[-120:])
        _append_log(tail)
    _append_log(f"exit={proc.returncode}")
    return proc.returncode, output


def _parse_time(value: str, *, end_of_day: bool = False) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if end_of_day and fmt == "%Y-%m-%d":
                dt = dt + timedelta(days=1) - timedelta(seconds=1)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def _format_time(ts: int | float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ""


def _safe_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value)


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _safe_json(value)


def _message_index_text(msg: dict) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def add(value) -> None:
        text = _stringify(value).strip()
        if not text or text in seen:
            return
        seen.add(text)
        parts.append(text)

    add(msg.get("content"))
    for key in (
        "transcription",
        "title",
        "description",
        "file_name",
        "filename",
        "url",
        "address",
        "location",
        "summary",
        "refer_text",
        "finder_desc",
    ):
        if key in msg:
            add(msg.get(key))

    # Keep uncommon structured fields searchable without indexing very large raw
    # blobs more than once.
    for key, value in msg.items():
        if key in {
            "local_id",
            "timestamp",
            "sender",
            "type",
            "content",
            "transcription",
            "raw",
        }:
            continue
        if isinstance(value, (dict, list)):
            add(value)
    return "\n".join(parts)


def _message_id(username: str, source_file: str, msg: dict, text: str) -> str:
    payload = "|".join(
        [
            username or "",
            os.path.basename(source_file or ""),
            str(msg.get("local_id", "")),
            str(msg.get("timestamp", "")),
            str(msg.get("sender", "")),
            hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _iter_export_json_files(export_dir: str):
    if not os.path.isdir(export_dir):
        return
    for name in sorted(os.listdir(export_dir)):
        lower = name.lower()
        if not lower.endswith(".json"):
            continue
        if name.startswith("_") or lower.endswith(".delta.json"):
            continue
        path = os.path.join(export_dir, name)
        if os.path.isfile(path):
            yield path


def _detect_fts_tokenizer(conn: sqlite3.Connection) -> str:
    for tokenizer in ("trigram", "unicode61"):
        try:
            conn.execute("DROP TABLE IF EXISTS _fts_probe")
            conn.execute(
                "CREATE VIRTUAL TABLE _fts_probe USING fts5("
                f"content, tokenize='{tokenizer}'"
                ")"
            )
            conn.execute("DROP TABLE _fts_probe")
            return tokenizer
        except sqlite3.Error:
            try:
                conn.execute("DROP TABLE IF EXISTS _fts_probe")
            except sqlite3.Error:
                pass
    return ""


def _connect_index() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(INDEX_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_index_schema(conn: sqlite3.Connection) -> str:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            chat TEXT NOT NULL,
            is_group INTEGER NOT NULL DEFAULT 0,
            local_id INTEGER,
            timestamp INTEGER,
            datetime TEXT,
            sender TEXT,
            type TEXT,
            content TEXT,
            source_file TEXT,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_summaries (
            chat_key TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            chat TEXT NOT NULL,
            is_group INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            text_message_count INTEGER NOT NULL DEFAULT 0,
            participant_count INTEGER NOT NULL DEFAULT 0,
            active_days INTEGER NOT NULL DEFAULT 0,
            first_timestamp INTEGER,
            last_timestamp INTEGER,
            top_sender TEXT,
            top_keywords_json TEXT NOT NULL DEFAULT '[]',
            top_types_json TEXT NOT NULL DEFAULT '[]',
            top_senders_json TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_index (
            chat_key TEXT NOT NULL,
            term TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            sample TEXT,
            PRIMARY KEY (chat_key, term)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_username ON messages(username)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_keyword_term ON keyword_index(term, count DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_summaries_last_ts ON chat_summaries(last_timestamp DESC)"
    )

    tokenizer = conn.execute(
        "SELECT value FROM meta WHERE key='fts_tokenizer'"
    ).fetchone()
    tokenizer_value = tokenizer["value"] if tokenizer else ""
    if not tokenizer_value:
        tokenizer_value = _detect_fts_tokenizer(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_tokenizer', ?)",
            (tokenizer_value,),
        )
    if tokenizer_value:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                "chat, sender, content, username, "
                f"tokenize='{tokenizer_value}'"
                ")"
            )
        except sqlite3.Error:
            tokenizer_value = ""
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('fts_tokenizer', '')"
            )
    return tokenizer_value


def rebuild_index(export_dir: str = EXPORT_DIR) -> dict:
    started = time.time()
    _set_job(step="重建搜索索引")
    stats = {
        "files": 0,
        "messages": 0,
        "skipped_files": 0,
        "fts_tokenizer": "",
        "summary_chats": 0,
        "keyword_rows": 0,
        "elapsed_seconds": 0.0,
    }
    chat_accumulators: dict[str, ChatAccumulator] = {}
    with closing(_connect_index()) as conn:
        tokenizer = _init_index_schema(conn)
        stats["fts_tokenizer"] = tokenizer
        conn.execute("DELETE FROM messages")
        if tokenizer:
            conn.execute("DELETE FROM messages_fts")
        conn.execute("DELETE FROM chat_summaries")
        conn.execute("DELETE FROM keyword_index")

        for path in _iter_export_json_files(export_dir) or []:
            stats["files"] += 1
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                stats["skipped_files"] += 1
                continue
            if not isinstance(data, dict):
                stats["skipped_files"] += 1
                continue
            chat = _stringify(data.get("chat") or data.get("name") or "")
            username = _stringify(data.get("username") or "")
            is_group = bool(data.get("is_group") or str(username).endswith("@chatroom"))
            messages = data.get("messages") or []
            if not isinstance(messages, list):
                stats["skipped_files"] += 1
                continue
            key = chat_identity(username, chat)
            acc = chat_accumulators.get(key)
            if acc is None:
                acc = ChatAccumulator(username=username, chat=chat, is_group=is_group)
                chat_accumulators[key] = acc

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                text = _message_index_text(msg)
                if not text and not chat:
                    continue
                msg_id = _message_id(username, path, msg, text)
                timestamp = msg.get("timestamp")
                try:
                    timestamp_int = int(timestamp) if timestamp is not None else None
                except (TypeError, ValueError):
                    timestamp_int = None
                sender = _stringify(msg.get("sender"))
                msg_type = _stringify(msg.get("type") or "text")
                local_id = msg.get("local_id")
                try:
                    local_id_int = int(local_id) if local_id is not None else None
                except (TypeError, ValueError):
                    local_id_int = None
                cur = conn.execute(
                    """
                    INSERT OR REPLACE INTO messages (
                        id, username, chat, is_group, local_id, timestamp,
                        datetime, sender, type, content, source_file, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg_id,
                        username,
                        chat,
                        int(is_group),
                        local_id_int,
                        timestamp_int,
                        _format_time(timestamp_int),
                        sender,
                        msg_type,
                        text,
                        os.path.basename(path),
                        _safe_json(msg),
                    ),
                    )
                if tokenizer:
                    conn.execute(
                        """
                        INSERT INTO messages_fts(rowid, chat, sender, content, username)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (cur.lastrowid, chat, sender, text, username),
                    )
                acc.update(
                    sender=sender,
                    msg_type=msg_type,
                    timestamp=timestamp_int,
                    text=text,
                )
                stats["messages"] += 1

        for key, acc in chat_accumulators.items():
            summary = acc.finalize()
            conn.execute(
                """
                INSERT INTO chat_summaries (
                    chat_key, username, chat, is_group, message_count,
                    text_message_count, participant_count, active_days,
                    first_timestamp, last_timestamp, top_sender,
                    top_keywords_json, top_types_json, top_senders_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    summary["username"],
                    summary["chat"],
                    summary["is_group"],
                    summary["message_count"],
                    summary["text_message_count"],
                    summary["participant_count"],
                    summary["active_days"],
                    summary["first_timestamp"],
                    summary["last_timestamp"],
                    summary["top_sender"],
                    json.dumps(summary["top_keywords"], ensure_ascii=False),
                    json.dumps(summary["top_types"], ensure_ascii=False),
                    json.dumps(summary["top_senders"], ensure_ascii=False),
                ),
            )
            stats["summary_chats"] += 1
            for item in summary["top_keywords"]:
                conn.execute(
                    """
                    INSERT INTO keyword_index(chat_key, term, count, sample)
                    VALUES (?, ?, ?, ?)
                    """,
                    (key, item["term"], item["count"], item.get("sample", "")),
                )
                stats["keyword_rows"] += 1

        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_indexed_at', ?)",
            (_now_text(),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('message_count', ?)",
            (str(stats["messages"]),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('summary_chat_count', ?)",
            (str(stats["summary_chats"]),),
        )
        conn.commit()

    stats["elapsed_seconds"] = round(time.time() - started, 2)
    _update_settings(
        last_indexed_at=_now_text(),
        last_backup_summary=(
            f"索引 {stats['messages']} 条消息，{stats['files']} 个会话文件，"
            f"生成 {stats['summary_chats']} 个会话摘要"
        ),
    )
    _append_log(
        f"index rebuilt: {stats['messages']} messages from {stats['files']} files; "
        f"{stats['summary_chats']} chat summaries; {stats['keyword_rows']} keywords"
    )
    return stats


def _escape_fts_term(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _build_fts_query(query: str) -> str:
    parts = [p for p in re.split(r"\s+", query.strip()) if p]
    if not parts:
        return ""
    return " AND ".join(_escape_fts_term(p) for p in parts)


def _row_to_result(row: sqlite3.Row) -> dict:
    content = row["content"] or ""
    return {
        "id": row["id"],
        "chat": row["chat"],
        "username": row["username"],
        "sender": row["sender"],
        "type": row["type"],
        "timestamp": row["timestamp"],
        "datetime": row["datetime"],
        "content": content,
        "source_file": row["source_file"],
    }


def _fetch_related_context(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    limit_each_side: int = 2,
) -> list[dict]:
    timestamp = row["timestamp"]
    username = row["username"]
    local_id = row["local_id"] if row["local_id"] is not None else -1
    if timestamp is None:
        return []

    before_rows = conn.execute(
        """
        SELECT id, datetime, sender, type, content
        FROM messages
        WHERE username = ?
          AND timestamp IS NOT NULL
          AND (
            timestamp < ?
            OR (timestamp = ? AND COALESCE(local_id, -1) < ?)
          )
        ORDER BY timestamp DESC, COALESCE(local_id, -1) DESC
        LIMIT ?
        """,
        (username, timestamp, timestamp, local_id, limit_each_side),
    ).fetchall()
    after_rows = conn.execute(
        """
        SELECT id, datetime, sender, type, content
        FROM messages
        WHERE username = ?
          AND timestamp IS NOT NULL
          AND (
            timestamp > ?
            OR (timestamp = ? AND COALESCE(local_id, -1) > ?)
          )
        ORDER BY timestamp ASC, COALESCE(local_id, -1) ASC
        LIMIT ?
        """,
        (username, timestamp, timestamp, local_id, limit_each_side),
    ).fetchall()

    items = list(reversed(before_rows)) + list(after_rows)
    return [
        {
            "id": item["id"],
            "datetime": item["datetime"] or "",
            "sender": item["sender"] or "",
            "type": item["type"] or "",
            "content": short_snippet(item["content"] or "", 180),
        }
        for item in items
    ]


def _fetch_chat_summary(conn: sqlite3.Connection, username: str, chat: str) -> dict | None:
    key = chat_identity(username, chat)
    row = conn.execute(
        "SELECT * FROM chat_summaries WHERE chat_key = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    summary = dict(row)
    summary["top_keywords"] = decode_json_list(summary.pop("top_keywords_json", "[]"))
    summary["top_types"] = decode_json_list(summary.pop("top_types_json", "[]"))
    summary["top_senders"] = decode_json_list(summary.pop("top_senders_json", "[]"))
    return summary


def _fetch_keyword_matches(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = KNOWLEDGE_NEIGHBOR_LIMIT,
) -> list[dict]:
    terms = related_query_terms(query, limit=4)
    if not terms:
        return []
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for term in terms:
        for row in conn.execute(
            """
            SELECT k.chat_key, k.term, k.count, k.sample, s.chat, s.username
            FROM keyword_index k
            JOIN chat_summaries s ON s.chat_key = k.chat_key
            WHERE k.term LIKE ?
            ORDER BY k.count DESC, s.last_timestamp DESC
            LIMIT ?
            """,
            (f"%{term}%", limit),
        ).fetchall():
            pair = (row["chat_key"], row["term"])
            if pair in seen:
                continue
            seen.add(pair)
            rows.append(
                {
                    "chat": row["chat"] or row["username"] or "",
                    "username": row["username"] or "",
                    "term": row["term"] or "",
                    "count": int(row["count"] or 0),
                    "sample": row["sample"] or "",
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


def _related_chats_from_items(
    conn: sqlite3.Connection,
    items: list[dict],
    *,
    limit: int = KNOWLEDGE_NEIGHBOR_LIMIT,
) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in items:
        key = chat_identity(item.get("username", ""), item.get("chat", ""))
        entry = grouped.get(key)
        if entry is None:
            summary = _fetch_chat_summary(conn, item.get("username", ""), item.get("chat", ""))
            entry = {
                "chat": item.get("chat") or item.get("username") or "",
                "username": item.get("username") or "",
                "hits": 0,
                "latest_datetime": item.get("datetime") or "",
                "latest_snippet": short_snippet(item.get("content") or "", 120),
                "top_keywords": [],
            }
            if summary:
                entry["top_keywords"] = [
                    keyword.get("term", "")
                    for keyword in (summary.get("top_keywords") or [])[:5]
                    if keyword.get("term")
                ]
            grouped[key] = entry
        entry["hits"] += 1
        if item.get("datetime") and item.get("datetime") > entry["latest_datetime"]:
            entry["latest_datetime"] = item.get("datetime") or ""
            entry["latest_snippet"] = short_snippet(item.get("content") or "", 120)

    ordered = sorted(
        grouped.values(),
        key=lambda value: (-value["hits"], value["latest_datetime"]),
        reverse=False,
    )
    return ordered[:limit]


def search_index(
    query: str,
    *,
    chat: str = "",
    start: str = "",
    end: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    query = (query or "").strip()
    chat = (chat or "").strip()
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    start_ts = _parse_time(start)
    end_ts = _parse_time(end, end_of_day=True)
    if not os.path.exists(INDEX_DB):
        return {"items": [], "total": 0, "error": "索引不存在，请先备份或重建索引"}

    def like_search(conn: sqlite3.Connection) -> tuple[list[dict], int]:
        where = list(filters)
        like_params = list(params)
        if query:
            where.append(
                "(m.content LIKE ? OR m.chat LIKE ? OR m.sender LIKE ? OR m.username LIKE ?)"
            )
            like = f"%{query}%"
            like_params.extend([like, like, like, like])
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM messages m {where_sql}", like_params
        ).fetchone()
        like_total = int(count_row["n"] if count_row else 0)
        rows = conn.execute(
            f"""
            SELECT m.*
            FROM messages m
            {where_sql}
            ORDER BY m.timestamp DESC, m.local_id DESC
            LIMIT ? OFFSET ?
            """,
            like_params + [limit, offset],
        ).fetchall()
        return [_row_to_result(row) for row in rows], like_total

    with closing(_connect_index()) as conn:
        tokenizer_row = conn.execute(
            "SELECT value FROM meta WHERE key='fts_tokenizer'"
        ).fetchone()
        tokenizer = tokenizer_row["value"] if tokenizer_row else ""
        params: list[object] = []
        filters = []
        if chat:
            filters.append("(m.chat LIKE ? OR m.username LIKE ?)")
            params.extend([f"%{chat}%", f"%{chat}%"])
        if start_ts is not None:
            filters.append("m.timestamp >= ?")
            params.append(start_ts)
        if end_ts is not None:
            filters.append("m.timestamp <= ?")
            params.append(end_ts)

        items: list[dict] = []
        total = 0
        used_fts = False
        if query and tokenizer:
            fts_query = _build_fts_query(query)
            where = ["messages_fts MATCH ?"]
            fts_params: list[object] = [fts_query]
            if filters:
                where.extend(filters)
                fts_params.extend(params)
            where_sql = " AND ".join(where)
            try:
                count_row = conn.execute(
                    f"""
                    SELECT COUNT(*) AS n
                    FROM messages_fts f
                    JOIN messages m ON m.rowid = f.rowid
                    WHERE {where_sql}
                    """,
                    fts_params,
                ).fetchone()
                total = int(count_row["n"] if count_row else 0)
                rows = conn.execute(
                    f"""
                    SELECT m.*
                    FROM messages_fts f
                    JOIN messages m ON m.rowid = f.rowid
                    WHERE {where_sql}
                    ORDER BY m.timestamp DESC, m.local_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    fts_params + [limit, offset],
                ).fetchall()
                items = [_row_to_result(row) for row in rows]
                used_fts = True
            except sqlite3.Error:
                used_fts = False

            # SQLite trigram FTS is fast for longer substrings but can miss short
            # Chinese terms such as "合同". Product behavior should prefer a
            # complete local result set over a fast empty result.
            if used_fts and total == 0:
                items, total = like_search(conn)
                used_fts = False

        if not used_fts:
            items, total = like_search(conn)
        for item in items:
            row = conn.execute(
                "SELECT * FROM messages WHERE id = ?",
                (item["id"],),
            ).fetchone()
            if row:
                item["context"] = _fetch_related_context(conn, row)
                item["chat_summary"] = _fetch_chat_summary(
                    conn,
                    item.get("username", ""),
                    item.get("chat", ""),
                )
            else:
                item["context"] = []
                item["chat_summary"] = None
        related = _related_chats_from_items(conn, items)
        if query and len(related) < KNOWLEDGE_NEIGHBOR_LIMIT:
            fallback = _fetch_keyword_matches(
                conn,
                query,
                limit=KNOWLEDGE_NEIGHBOR_LIMIT - len(related),
            )
            existing = {
                (item.get("username", ""), item.get("chat", ""), item.get("term", ""))
                for item in related
            }
            for item in fallback:
                marker = (item.get("username", ""), item.get("chat", ""), item.get("term", ""))
                if marker in existing:
                    continue
                related.append(item)
                if len(related) >= KNOWLEDGE_NEIGHBOR_LIMIT:
                    break
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "engine": "fts5" if used_fts else "like",
        "related_chats": related,
    }


def _index_stats() -> dict:
    if not os.path.exists(INDEX_DB):
        return {
            "exists": False,
            "message_count": 0,
            "chat_count": 0,
            "summary_chat_count": 0,
            "last_indexed_at": "",
            "fts_tokenizer": "",
        }
    try:
        with closing(_connect_index()) as conn:
            _init_index_schema(conn)
            msg_row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
            chat_row = conn.execute(
                "SELECT COUNT(DISTINCT username) AS n FROM messages"
            ).fetchone()
            meta = {
                row["key"]: row["value"]
                for row in conn.execute("SELECT key, value FROM meta").fetchall()
            }
            return {
                "exists": True,
                "message_count": int(msg_row["n"] if msg_row else 0),
                "chat_count": int(chat_row["n"] if chat_row else 0),
                "summary_chat_count": int(meta.get("summary_chat_count", "0") or 0),
                "last_indexed_at": meta.get("last_indexed_at", ""),
                "fts_tokenizer": meta.get("fts_tokenizer", ""),
            }
    except sqlite3.Error as e:
        return {
            "exists": False,
            "message_count": 0,
            "chat_count": 0,
            "summary_chat_count": 0,
            "error": str(e),
        }


def _latest_logs(max_lines: int = 160) -> str:
    if not os.path.exists(LOG_FILE):
        return ""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    return "".join(lines[-max_lines:])


def run_backup(manual: bool = False) -> dict:
    if not job_lock.acquire(blocking=False):
        return {"started": False, "error": "已有备份或索引任务正在运行"}
    if not _acquire_task_lock():
        job_lock.release()
        return {"started": False, "error": "已有其他进程正在备份或重建索引"}
    try:
        settings = _load_settings()
        _set_job(
            running=True,
            kind="backup",
            started_at=_now_text(),
            finished_at="",
            step="准备备份",
            ok=None,
            message="",
        )
        _append_log("backup started")
        _ensure_dirs()

        decrypt_cmd, export_cmd = _backup_commands(settings)
        _set_job(step="增量解密微信数据库")
        code, output = _run_command(decrypt_cmd)
        if code != 0:
            raise RuntimeError(_tail_error(output) or f"解密失败，退出码 {code}")

        _set_job(step="增量导出聊天记录")
        code, output = _run_command(export_cmd)
        if code != 0:
            raise RuntimeError(_tail_error(output) or f"导出失败，退出码 {code}")

        stats = rebuild_index(EXPORT_DIR)
        summary = (
            f"备份完成：索引 {stats['messages']} 条消息，"
            f"{stats['files']} 个会话文件"
        )
        _update_settings(
            last_backup_at=_now_text(),
            last_backup_ok=True,
            last_backup_summary=summary,
            last_error="",
        )
        _set_job(
            running=False,
            finished_at=_now_text(),
            step="完成",
            ok=True,
            message=summary,
        )
        _append_log(summary)
        return {"started": True, "ok": True, "summary": summary, "index": stats}
    except Exception as e:
        err = str(e)
        _append_log("backup failed: " + err)
        _append_log(traceback.format_exc())
        _update_settings(
            last_backup_at=_now_text(),
            last_backup_ok=False,
            last_error=err,
            last_backup_summary="备份失败",
        )
        _set_job(
            running=False,
            finished_at=_now_text(),
            step="失败",
            ok=False,
            message=err,
        )
        return {"started": True, "ok": False, "error": err}
    finally:
        _release_task_lock()
        job_lock.release()


def run_index_only() -> dict:
    if not job_lock.acquire(blocking=False):
        return {"started": False, "error": "已有备份或索引任务正在运行"}
    if not _acquire_task_lock():
        job_lock.release()
        return {"started": False, "error": "已有其他进程正在备份或重建索引"}
    try:
        _set_job(
            running=True,
            kind="index",
            started_at=_now_text(),
            finished_at="",
            step="重建搜索索引",
            ok=None,
            message="",
        )
        stats = rebuild_index(EXPORT_DIR)
        summary = f"索引完成：{stats['messages']} 条消息，{stats['files']} 个会话文件"
        _set_job(
            running=False,
            finished_at=_now_text(),
            step="完成",
            ok=True,
            message=summary,
        )
        return {"started": True, "ok": True, "summary": summary, "index": stats}
    except Exception as e:
        err = str(e)
        _append_log("index failed: " + err)
        _append_log(traceback.format_exc())
        _set_job(
            running=False,
            finished_at=_now_text(),
            step="失败",
            ok=False,
            message=err,
        )
        return {"started": True, "ok": False, "error": err}
    finally:
        _release_task_lock()
        job_lock.release()


def _tail_error(output: str) -> str:
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-8:])


def start_background_backup(manual: bool = False) -> dict:
    if job_state.get("running"):
        return {"started": False, "error": "已有备份或索引任务正在运行"}
    t = threading.Thread(target=run_backup, kwargs={"manual": manual}, daemon=True)
    t.start()
    return {"started": True}


def start_background_index() -> dict:
    if job_state.get("running"):
        return {"started": False, "error": "已有备份或索引任务正在运行"}
    t = threading.Thread(target=run_index_only, daemon=True)
    t.start()
    return {"started": True}


def scheduler_loop() -> None:
    while not scheduler_stop.wait(30):
        settings = _load_settings()
        if not settings.get("auto_backup_enabled"):
            continue
        if job_state.get("running"):
            continue
        last = _parse_time(settings.get("last_backup_at", ""))
        interval = max(
            MIN_INTERVAL_MINUTES, int(settings.get("backup_interval_minutes") or 30)
        )
        due = last is None or time.time() - last >= interval * 60
        if due:
            _append_log("scheduled backup due")
            start_background_backup(manual=False)


def app_status() -> dict:
    settings = _load_settings()
    with state_lock:
        job = dict(job_state)
    return {
        "settings": settings,
        "job": job,
        "index": _index_stats(),
        "paths": {
            "data_dir": DATA_DIR,
            "export_dir": EXPORT_DIR,
            "index_db": INDEX_DB,
            "log_file": LOG_FILE,
        },
        "background_agent": _background_state(),
        "server": {"host": HOST, "port": PORT},
        "time": _now_text(),
    }


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信本地备份搜索</title>
<style>
:root{
  --ink:#17211b;--muted:#67746b;--paper:#f4efe4;--card:#fffaf0;
  --line:#ded2bd;--green:#1f6f4a;--green-2:#dceee3;--amber:#b86b22;
  --red:#a64037;--shadow:0 22px 70px rgba(42,33,18,.16);
}
*{box-sizing:border-box}
body{
  margin:0;color:var(--ink);font-family:"Noto Serif SC","Microsoft YaHei",serif;
  background:
    radial-gradient(circle at 8% 10%,rgba(31,111,74,.16),transparent 32rem),
    radial-gradient(circle at 82% 4%,rgba(184,107,34,.16),transparent 34rem),
    linear-gradient(135deg,#f8f3e8,#e9e1d1 58%,#f7efe1);
  min-height:100vh;
}
.wrap{max-width:1180px;margin:0 auto;padding:34px 20px 48px}
.hero{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;align-items:stretch}
.panel{
  background:rgba(255,250,240,.82);border:1px solid rgba(128,103,70,.24);
  border-radius:28px;box-shadow:var(--shadow);backdrop-filter:blur(12px);
}
.title{padding:34px}
h1{font-size:42px;line-height:1.05;margin:0 0 14px;letter-spacing:-1px}
.sub{font-size:15px;line-height:1.8;color:var(--muted);max-width:720px}
.status{padding:26px;display:grid;gap:14px}
.pill{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:8px 12px;background:var(--green-2);color:var(--green);font-size:13px;font-weight:700}
.dot{width:9px;height:9px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(31,111,74,.12)}
.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.metric{background:#fff;border:1px solid var(--line);border-radius:18px;padding:14px}
.metric b{display:block;font-size:24px;margin-bottom:4px}
.metric span{font-size:12px;color:var(--muted)}
.controls,.searchbox,.logs{margin-top:18px;padding:22px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button{
  appearance:none;border:0;border-radius:15px;background:var(--ink);color:#fff;
  padding:12px 16px;font-weight:700;cursor:pointer;box-shadow:0 10px 22px rgba(23,33,27,.16)
}
button.secondary{background:#fff;color:var(--ink);border:1px solid var(--line);box-shadow:none}
button.warn{background:var(--amber)}
button:disabled{opacity:.55;cursor:not-allowed}
input[type="text"],input[type="number"],input[type="date"]{
  border:1px solid var(--line);background:#fff;border-radius:14px;padding:12px 13px;
  font:inherit;min-height:44px;outline:none;
}
input[type="text"]:focus,input[type="number"]:focus,input[type="date"]:focus{border-color:var(--green);box-shadow:0 0 0 4px rgba(31,111,74,.12)}
.searchline{display:grid;grid-template-columns:1.5fr .8fr .45fr .45fr auto;gap:10px}
.hint{font-size:12px;color:var(--muted);margin-top:10px}
.results{display:grid;gap:12px;margin-top:16px}
.result{background:#fff;border:1px solid var(--line);border-radius:20px;padding:16px;animation:rise .25s ease both}
.result-head{display:flex;gap:10px;align-items:center;justify-content:space-between;color:var(--muted);font-size:12px;margin-bottom:8px}
.chat{font-weight:800;color:var(--green)}
.content{white-space:pre-wrap;line-height:1.7;font-size:14px}
.tag{border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:#fffaf4}
pre{max-height:260px;overflow:auto;background:#141914;color:#d9eadc;border-radius:18px;padding:14px;font-size:12px;line-height:1.6}
label{font-size:14px;color:var(--muted)}
.check{display:inline-flex;gap:8px;align-items:center}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media (max-width:880px){
  .hero{grid-template-columns:1fr}.searchline{grid-template-columns:1fr}
  h1{font-size:32px}.metrics{grid-template-columns:1fr}
}
</style>
</head>
<body>
<main class="wrap">
  <section class="hero">
    <div class="panel title">
      <div class="pill"><i class="dot"></i>本机私有备份</div>
      <h1>微信记录备份<br>和快速搜索</h1>
      <p class="sub">数据只保存在本机。点击“立即备份”会调用本地解密和增量导出脚本，然后把 JSON 聊天记录写入 SQLite 搜索索引。</p>
    </div>
    <div class="panel status">
      <div class="metrics">
        <div class="metric"><b id="msgCount">0</b><span>已索引消息</span></div>
        <div class="metric"><b id="chatCount">0</b><span>会话数量</span></div>
        <div class="metric"><b id="jobState">空闲</b><span>当前任务</span></div>
        <div class="metric"><b id="engine">-</b><span>搜索引擎</span></div>
      </div>
      <div class="hint" id="lastInfo">正在读取状态...</div>
    </div>
  </section>

  <section class="panel controls">
    <div class="row">
      <button id="backupBtn" onclick="startBackup()">立即备份</button>
      <button class="secondary" id="indexBtn" onclick="startIndex()">只重建索引</button>
      <label class="check"><input type="checkbox" id="autoEnabled"> 自动备份</label>
      <label>间隔 <input type="number" id="interval" min="1" max="1440" value="30" style="width:88px"> 分钟</label>
      <label class="check"><input type="checkbox" id="withTx"> 备份时转录语音</label>
      <button class="secondary" onclick="saveSettings()">保存设置</button>
    </div>
    <div class="hint">首次备份需要微信已登录运行，并且 Windows 需要管理员权限。后续可按间隔自动增量备份。</div>
  </section>

  <section class="panel searchbox">
    <div class="searchline">
      <input id="q" type="text" placeholder="搜索聊天记录，例如：合同、地址、发票、某句话" onkeydown="if(event.key==='Enter') search()">
      <input id="chat" type="text" placeholder="联系人 / 群名，可空" onkeydown="if(event.key==='Enter') search()">
      <input id="start" type="date">
      <input id="end" type="date">
      <button onclick="search()">搜索</button>
    </div>
    <div class="hint" id="searchMeta">输入关键词后搜索。中文关键词会优先使用本地全文索引，必要时回退到 LIKE。</div>
    <div class="results" id="results"></div>
  </section>

  <section class="panel logs">
    <div class="row" style="justify-content:space-between">
      <strong>运行日志</strong>
      <button class="secondary" onclick="refreshLogs()">刷新日志</button>
    </div>
    <pre id="logs"></pre>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]))}
async function api(path, opts){const r=await fetch(path,opts);return await r.json()}
async function refreshStatus(){
  const s=await api('/api/status');
  $('msgCount').textContent=s.index.message_count||0;
  $('chatCount').textContent=s.index.chat_count||0;
  $('jobState').textContent=s.job.running ? s.job.step : (s.job.ok===false?'失败':'空闲');
  $('engine').textContent=s.index.fts_tokenizer||'LIKE';
  $('backupBtn').disabled=!!s.job.running;
  $('indexBtn').disabled=!!s.job.running;
  $('autoEnabled').checked=!!s.settings.auto_backup_enabled;
  $('withTx').checked=!!s.settings.with_transcriptions;
  $('interval').value=s.settings.backup_interval_minutes||30;
  const last=s.settings.last_backup_at||'从未备份';
  const idx=s.settings.last_indexed_at||s.index.last_indexed_at||'从未索引';
  $('lastInfo').textContent=`上次备份：${last}；上次索引：${idx}；${s.job.message||s.settings.last_error||s.settings.last_backup_summary||''}`;
}
async function startBackup(){
  await api('/api/backup',{method:'POST'});
  refreshStatus(); refreshLogs();
}
async function startIndex(){
  await api('/api/index',{method:'POST'});
  refreshStatus(); refreshLogs();
}
async function saveSettings(){
  const payload={
    auto_backup_enabled:$('autoEnabled').checked,
    backup_interval_minutes:Number($('interval').value||30),
    with_transcriptions:$('withTx').checked
  };
  await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  refreshStatus();
}
async function search(){
  const params=new URLSearchParams({
    q:$('q').value,chat:$('chat').value,start:$('start').value,end:$('end').value,limit:'50'
  });
  const data=await api('/api/search?'+params.toString());
  $('searchMeta').textContent=data.error ? data.error : `找到 ${data.total} 条，当前显示 ${data.items.length} 条，引擎：${data.engine}`;
  $('results').innerHTML=data.items.map(x=>`
    <article class="result">
      <div class="result-head">
        <span><span class="chat">${esc(x.chat||x.username)}</span> · ${esc(x.sender||'')}</span>
        <span><span class="tag">${esc(x.type||'text')}</span> ${esc(x.datetime||'')}</span>
      </div>
      <div class="content">${esc((x.content||'').slice(0,1200))}</div>
    </article>
  `).join('') || '<div class="hint">没有结果。</div>';
}
async function refreshLogs(){
  const data=await api('/api/logs');
  $('logs').textContent=data.logs||'暂无日志';
}
refreshStatus(); refreshLogs();
setInterval(refreshStatus,3000);
setInterval(refreshLogs,10000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatBackupSearch/0.1"

    def log_message(self, fmt, *args):
        return

    def _is_local(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1", "localhost"}

    def _send_json(self, data, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        if not self._is_local():
            self.send_error(403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            payload = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if path == "/api/status":
            self._send_json(app_status())
            return
        if path == "/api/logs":
            self._send_json({"logs": _latest_logs()})
            return
        if path == "/api/search":
            params = urllib.parse.parse_qs(parsed.query)
            result = search_index(
                params.get("q", [""])[0],
                chat=params.get("chat", [""])[0],
                start=params.get("start", [""])[0],
                end=params.get("end", [""])[0],
                limit=int(params.get("limit", ["50"])[0] or 50),
                offset=int(params.get("offset", ["0"])[0] or 0),
            )
            self._send_json(result)
            return
        self.send_error(404)

    def do_POST(self):
        if not self._is_local():
            self.send_error(403)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/backup":
            self._send_json(start_background_backup(manual=True))
            return
        if parsed.path == "/api/index":
            self._send_json(start_background_index())
            return
        if parsed.path == "/api/settings":
            data = self._read_json()
            interval = data.get("backup_interval_minutes", 30)
            try:
                interval = int(interval)
            except (TypeError, ValueError):
                interval = 30
            settings = _update_settings(
                auto_backup_enabled=bool(data.get("auto_backup_enabled")),
                backup_interval_minutes=max(MIN_INTERVAL_MINUTES, interval),
                with_transcriptions=bool(data.get("with_transcriptions")),
            )
            self._send_json({"ok": True, "settings": settings})
            return
        self.send_error(404)


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    _ensure_dirs()
    _append_log("app started")
    scheduler = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler.start()
    server = ThreadedServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 60, flush=True)
    print("  WeChat Backup Search", flush=True)
    print("=" * 60, flush=True)
    print(f"=> {url}", flush=True)
    print(f"数据目录: {DATA_DIR}", flush=True)
    print("Ctrl+C 停止", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        scheduler_stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
