#!/usr/bin/env python3
"""Decrypt Windows WeChat 3.x SQLCipher databases.

This is intentionally small and local-only.  It expects a 32-byte raw WeChat
DB key and handles the Windows 3.x format verified on WeChat 3.9.12:
SQLCipher 3, 4096-byte pages, PBKDF2-SHA1, reserve 48.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import struct
import sys
from pathlib import Path

from Crypto.Cipher import AES


SQLITE_HDR = b"SQLite format 3\x00"
PAGE_SIZE = 4096
RESERVE = 48
KEY_SIZE = 32


def derive_key(raw_key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", raw_key, salt, 64000, KEY_SIZE)


def verify_page1(page1: bytes, enc_key: bytes) -> bool:
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha1", enc_key, mac_salt, 2, KEY_SIZE)
    content = page1[16 : PAGE_SIZE - RESERVE]
    iv = page1[PAGE_SIZE - RESERVE : PAGE_SIZE - RESERVE + 16]
    stored = page1[PAGE_SIZE - RESERVE + 16 : PAGE_SIZE - RESERVE + 36]
    msg = content + iv + struct.pack("<I", 1)
    return hmac.new(mac_key, msg, hashlib.sha1).digest() == stored


def decrypt_page(page: bytes, enc_key: bytes, page_no: int) -> bytes:
    if len(page) < PAGE_SIZE:
        page = page + b"\x00" * (PAGE_SIZE - len(page))
    iv = page[PAGE_SIZE - RESERVE : PAGE_SIZE - RESERVE + 16]
    encrypted = page[16 : PAGE_SIZE - RESERVE] if page_no == 1 else page[: PAGE_SIZE - RESERVE]
    decrypted = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    if page_no == 1:
        # Keep header byte 20 (reserved bytes per page) from the decrypted
        # SQLite header. Windows WeChat 3.x pages are laid out with 48 reserved
        # bytes; clearing this byte makes SQLite scan into the reserve area and
        # can cause "database disk image is malformed" on larger queries.
        return SQLITE_HDR + decrypted + page[PAGE_SIZE - RESERVE : PAGE_SIZE]
    return decrypted + page[PAGE_SIZE - RESERVE : PAGE_SIZE]


def sqlite_check(path: Path) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(str(path))
        try:
            check_rows = conn.execute("PRAGMA quick_check").fetchall()
            if not check_rows or any(row[0] != "ok" for row in check_rows):
                return False, "; ".join(str(row[0]) for row in check_rows[:5])
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            names = [row[0] for row in tables]
            if "MSG" in names:
                conn.execute("SELECT COUNT(*) FROM MSG").fetchone()
                conn.execute(
                    "SELECT localId FROM MSG ORDER BY CreateTime DESC LIMIT 1"
                ).fetchone()
        finally:
            conn.close()
        return True, ",".join(names[:10]) + (f" ...共{len(names)}个" if len(names) > 10 else "")
    except sqlite3.Error as e:
        return False, str(e)


def decrypt_database(db_path: Path, out_path: Path, raw_key: bytes, *, incremental: bool = False) -> bool:
    if incremental and out_path.exists() and db_path.stat().st_mtime <= out_path.stat().st_mtime:
        print(f"SKIP: {db_path} (未修改)", flush=True)
        return True

    with db_path.open("rb") as f:
        first_page = f.read(PAGE_SIZE)
    if first_page.startswith(SQLITE_HDR):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, out_path)
        print(f"COPY: {db_path} -> {out_path}", flush=True)
        return True
    if len(first_page) < PAGE_SIZE:
        print(f"FAIL: {db_path} 文件太小", flush=True)
        return False

    enc_key = derive_key(raw_key, first_page[:16])
    if not verify_page1(first_page, enc_key):
        print(f"FAIL: {db_path} HMAC验证失败", flush=True)
        return False

    total_pages = (db_path.stat().st_size + PAGE_SIZE - 1) // PAGE_SIZE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with db_path.open("rb") as fin, tmp_path.open("wb") as fout:
        for page_no in range(1, total_pages + 1):
            page = fin.read(PAGE_SIZE)
            if not page:
                break
            fout.write(decrypt_page(page, enc_key, page_no))
    ok, info = sqlite_check(tmp_path)
    if not ok:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        print(f"FAIL: {db_path} SQLite校验失败: {info}", flush=True)
        return False
    tmp_path.replace(out_path)
    os.utime(out_path, (db_path.stat().st_atime, db_path.stat().st_mtime))
    for suffix in ("-wal", "-shm"):
        residual = str(out_path) + suffix
        if os.path.exists(residual):
            try:
                os.remove(residual)
            except OSError:
                pass
    print(f"OK: {db_path} -> {out_path} 表: {info}", flush=True)
    return True


def load_raw_key(key_arg: str) -> bytes:
    value = key_arg.strip()
    if os.path.exists(value):
        data = json.loads(Path(value).read_text(encoding="utf-8"))
        value = data.get("raw_key") or data.get("key") or ""
    if len(value) != 64:
        raise ValueError("key must be 64 hex chars or a JSON file with raw_key")
    return bytes.fromhex(value)


def iter_db_files(input_path: Path):
    if input_path.is_file():
        yield input_path, input_path.name
        return
    for path in sorted(input_path.rglob("*.db")):
        if path.name.endswith(("-wal", "-shm")):
            continue
        yield path, str(path.relative_to(input_path))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Decrypt Windows WeChat 3.x DB files")
    parser.add_argument("-k", "--key", required=True, help="64-hex raw key or wechat_3x_key.json")
    parser.add_argument("-i", "--input", required=True, help="DB file or directory")
    parser.add_argument("-o", "--output", required=True, help="output DB file or directory")
    parser.add_argument("--incremental", action="store_true", help="skip unchanged outputs")
    args = parser.parse_args(argv)

    raw_key = load_raw_key(args.key)
    input_path = Path(args.input)
    output_path = Path(args.output)
    if not input_path.exists():
        print(f"[ERROR] input not found: {input_path}", flush=True)
        return 1

    success = 0
    failed = 0
    items = list(iter_db_files(input_path))
    for db_path, rel in items:
        out_path = output_path if input_path.is_file() else output_path / rel
        if decrypt_database(db_path, out_path, raw_key, incremental=args.incremental):
            success += 1
        else:
            failed += 1
    print(f"结果: {success} 成功, {failed} 失败, 共 {len(items)} 个", flush=True)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
