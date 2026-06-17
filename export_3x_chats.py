#!/usr/bin/env python3
"""Export Windows WeChat 3.x chats from decrypted SQLite databases.

Input layout:
  <decrypted_root>/MicroMsg.db
  <decrypted_root>/MSG*.db

The exporter writes one JSON file per chat under the requested output
directory.  It only emits local files; no network access.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path


MSG_TYPE_MAP = {
    (1, 0): "text",
    (3, 0): "image",
    (34, 0): "voice",
    (42, 0): "contact_card",
    (43, 0): "video",
    (47, 0): "sticker",
    (48, 0): "location",
    (49, 0): "link_or_file",
    (10000, 0): "system",
    (10000, 4): "system",
    (10000, 57): "system",
}

SYSTEM_RE = re.compile(r"(<sysmsg[\s\S]*?</sysmsg>|<sysmsg[\s\S]*?>)")


def _safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', "_", value or "").strip()
    return value or "unknown"


def _fmt_ts(ts) -> str:
    try:
        if ts is None:
            return ""
        ts = int(ts)
        if ts > 10_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _message_text(local_type: int, sub_type: int, content: str) -> tuple[str, str]:
    msg_type = MSG_TYPE_MAP.get((local_type, sub_type), f"type_{local_type}_{sub_type}")
    content = content or ""
    if local_type == 1:
        return "text", content
    if local_type == 3:
        return "image", "[图片]"
    if local_type == 34:
        return "voice", "[语音]"
    if local_type == 43:
        return "video", "[视频]"
    if local_type == 47:
        return "sticker", "[表情]"
    if local_type == 48:
        return "location", "[位置]"
    if local_type == 42:
        return "contact_card", "[名片]"
    if local_type == 10000:
        text = content.strip()
        if not text:
            return "system", "[系统消息]"
        m = SYSTEM_RE.search(text)
        if m:
            return "system", m.group(1)
        return "system", text
    if local_type == 49:
        return msg_type, content if content else "[卡片/文件]"
    return msg_type, content or f"[{msg_type}]"


def _load_contacts(micro_db: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not micro_db.exists():
        return names
    with closing(sqlite3.connect(str(micro_db))) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(Contact)").fetchall()}
        select_cols = ["UserName", "NickName", "Remark"]
        rows = conn.execute(
            "SELECT UserName, NickName, Remark FROM Contact WHERE UserName IS NOT NULL"
        ).fetchall()
        for username, nick, remark in rows:
            display = (remark or nick or username or "").strip()
            if display:
                names[username] = display
    return names


def _iter_msg_dbs(decrypted_root: Path):
    candidates = [
        decrypted_root / "Msg",
        decrypted_root,
    ]
    for base in candidates:
        micro = base / "MicroMsg.db"
        if micro.exists():
            multi_dir = base / "Multi"
            if multi_dir.is_dir():
                for path in sorted(multi_dir.glob("MSG*.db")):
                    yield path
                    continue
            for path in sorted(base.glob("MSG*.db")):
                yield path
            return
    for path in sorted((decrypted_root / "Multi").glob("MSG*.db")):
        yield path


def _read_messages(db_path: Path):
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row[1] for row in conn.execute("PRAGMA table_info(MSG)").fetchall()}
        if not {"localId", "CreateTime", "StrTalker", "StrContent"}.issubset(cols):
            raise RuntimeError(f"MSG schema missing in {db_path}")
        rows = conn.execute(
            """
            SELECT localId, TalkerId, MsgSvrID, Type, SubType, IsSender, CreateTime,
                   StrTalker, StrContent, DisplayContent, CompressContent, BytesExtra, BytesTrans
            FROM MSG
            ORDER BY CreateTime ASC, localId ASC
            """
        ).fetchall()
    for row in rows:
        yield db_path, row


def export_chats(decrypted_root: Path, output_dir: Path, incremental: bool = False) -> dict:
    micro_db = decrypted_root / "MicroMsg.db"
    if not micro_db.exists():
        micro_db = decrypted_root / "Msg" / "MicroMsg.db"
    names = _load_contacts(micro_db)
    chats: dict[str, dict] = {}
    stats = {"dbs": 0, "messages": 0, "chats": 0}

    for db_path in _iter_msg_dbs(decrypted_root):
        stats["dbs"] += 1
        for _src, row in _read_messages(db_path):
            username = row["StrTalker"] or ""
            if not username:
                continue
            display_name = names.get(username, username)
            chat = chats.setdefault(
                username,
                {
                    "chat": display_name,
                    "username": username,
                    "is_group": username.endswith("@chatroom"),
                    "messages": [],
                },
            )
            local_type = int(row["Type"] or 0)
            sub_type = int(row["SubType"] or 0)
            sender = "me" if int(row["IsSender"] or 0) else ""
            msg_type, text = _message_text(local_type, sub_type, row["StrContent"] or "")
            chat["messages"].append(
                {
                    "local_id": int(row["localId"] or 0),
                    "timestamp": int(row["CreateTime"] or 0),
                    "datetime": _fmt_ts(row["CreateTime"]),
                    "sender": sender,
                    "type": msg_type,
                    "content": text,
                }
            )
            stats["messages"] += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    for username, data in chats.items():
        data["messages"].sort(key=lambda m: (m.get("timestamp") or 0, m.get("local_id") or 0))
        if data["messages"]:
            data["date_first_msg"] = data["messages"][0]["datetime"][:10]
            data["date_last_msg"] = data["messages"][-1]["datetime"][:10]
        out_path = output_dir / f"{_safe_name(username)}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    stats["chats"] = len(chats)
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export Windows WeChat 3.x chats")
    parser.add_argument("decrypted_root", help="decrypted DB root")
    parser.add_argument("output_dir", help="output chat JSON directory")
    parser.add_argument("-i", "--incremental", action="store_true", help="compat flag")
    args = parser.parse_args(argv)
    stats = export_chats(Path(args.decrypted_root), Path(args.output_dir), incremental=args.incremental)
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
