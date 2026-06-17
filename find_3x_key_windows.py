#!/usr/bin/env python3
"""Windows WeChat 3.x key scanner.

The 3.x Windows client keeps the DB password in memory as raw bytes.  This
scanner follows the PyWxDump-style path that works for 3.x: find device-type
anchors in WeChatWin.dll, walk backwards through nearby pointer-sized slots,
read 32 bytes from each pointed address, and verify candidates against the
real SQLCipher page-1 HMAC in MicroMsg.db.

It does not dump chat content.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

from Crypto.Cipher import AES


PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
SQLITE_HDR = b"SQLite format 3\x00"
DEFAULT_OUT = Path("wechat_3x_key.json")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wt.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MBI),
    ctypes.c_size_t,
]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

psapi.EnumProcessModulesEx.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(wt.HMODULE),
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
    wt.DWORD,
]
psapi.EnumProcessModulesEx.restype = wt.BOOL
psapi.GetModuleFileNameExW.argtypes = [
    wt.HANDLE,
    wt.HMODULE,
    wt.LPWSTR,
    wt.DWORD,
]
psapi.GetModuleFileNameExW.restype = wt.DWORD
psapi.GetModuleInformation.argtypes = [
    wt.HANDLE,
    wt.HMODULE,
    ctypes.POINTER(MODULEINFO),
    wt.DWORD,
]
psapi.GetModuleInformation.restype = wt.BOOL


def open_process(pid: int):
    return kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)


def read_mem(handle, addr: int, size: int) -> bytes | None:
    if size <= 0:
        return None
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)
    )
    if not ok or n.value == 0:
        return None
    return buf.raw[: n.value]


def read_uint(handle, addr: int, addr_len: int) -> int | None:
    data = read_mem(handle, addr, addr_len)
    if not data or len(data) != addr_len:
        return None
    return int.from_bytes(data, "little", signed=False)


def enum_regions(handle):
    regions = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(
            handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
        ) == 0:
            break
        base = int(mbi.BaseAddress or 0)
        size = int(mbi.RegionSize or 0)
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < size < 512 * 1024 * 1024:
            regions.append((base, size, mbi.Protect))
        nxt = base + size
        if nxt <= addr:
            break
        addr = nxt
    return regions


def enum_modules(handle):
    needed = wt.DWORD(0)
    psapi.EnumProcessModulesEx(handle, None, 0, ctypes.byref(needed), 0x03)
    count = max(needed.value // ctypes.sizeof(wt.HMODULE), 1)
    modules = (wt.HMODULE * count)()
    if not psapi.EnumProcessModulesEx(
        handle,
        modules,
        ctypes.sizeof(modules),
        ctypes.byref(needed),
        0x03,
    ):
        return []
    out = []
    for mod in modules[: needed.value // ctypes.sizeof(wt.HMODULE)]:
        name_buf = ctypes.create_unicode_buffer(4096)
        psapi.GetModuleFileNameExW(handle, mod, name_buf, len(name_buf))
        info = MODULEINFO()
        psapi.GetModuleInformation(handle, mod, ctypes.byref(info), ctypes.sizeof(info))
        out.append((int(info.lpBaseOfDll or 0), int(info.SizeOfImage or 0), name_buf.value))
    return out


def tasklist_processes():
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.strip('"').split('","')
        if len(parts) < 5 or parts[0] != "WeChat.exe":
            continue
        try:
            pid = int(parts[1])
            mem_kb = int(parts[4].replace(",", "").replace(" K", "").strip() or "0")
        except ValueError:
            continue
        rows.append((pid, mem_kb, parts[0]))
    rows.sort(key=lambda item: item[1], reverse=True)
    return rows


def get_exe_bit(path: str | None) -> int:
    if not path:
        return 64
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return 64
            f.seek(60)
            pe_off = int.from_bytes(f.read(4), "little")
            f.seek(pe_off + 4)
            machine = int.from_bytes(f.read(2), "little")
        if machine == 0x14C:
            return 32
        return 64
    except OSError:
        return 64


def find_active_wechat_dir() -> Path:
    root = Path.home() / "Documents" / "WeChat Files"
    candidates = []
    for micro in root.glob("wxid_*/Msg/MicroMsg.db"):
        try:
            candidates.append((micro.stat().st_mtime, micro.parent.parent))
        except OSError:
            pass
    if not candidates:
        raise RuntimeError(f"No WeChat 3.x MicroMsg.db under {root}")
    candidates.sort(reverse=True)
    return candidates[0][1]


def load_target_pages(base_dir: Path):
    targets = [
        base_dir / "Msg" / "MicroMsg.db",
        base_dir / "Msg" / "FTSContact.db",
        base_dir / "Msg" / "Multi" / "MSG0.db",
        base_dir / "Msg" / "Multi" / "MSG5.db",
    ]
    pages = []
    for path in targets:
        if not path.exists() or path.stat().st_size < 4096:
            continue
        page = path.read_bytes()[:4096]
        if page.startswith(SQLITE_HDR):
            continue
        pages.append((str(path), page))
    if not pages:
        raise RuntimeError(f"No encrypted target DBs found under {base_dir / 'Msg'}")
    return pages


def verify_page1(page1: bytes, enc_key: bytes, page_size: int, reserve: int, hmac_algo="sha1") -> bool:
    if len(page1) < page_size or page_size <= reserve + 16:
        return False
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    digest_size = hashlib.new(hmac_algo).digest_size
    mac_key = hashlib.pbkdf2_hmac(hmac_algo, enc_key, mac_salt, 2, dklen=32)
    content = page1[16 : page_size - reserve]
    iv = page1[page_size - reserve : page_size - reserve + 16]
    stored = page1[page_size - reserve + 16 : page_size - reserve + 16 + digest_size]
    if len(iv) != 16 or len(stored) != digest_size:
        return False
    msg = content + iv + struct.pack("<I", 1)
    return hmac.new(mac_key, msg, getattr(hashlib, hmac_algo)).digest() == stored


def decrypt_page1_header(page1: bytes, enc_key: bytes, page_size: int, reserve: int) -> bytes | None:
    iv = page1[page_size - reserve : page_size - reserve + 16]
    encrypted = page1[16 : page_size - reserve]
    if len(encrypted) % 16:
        return None
    try:
        dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    except Exception:
        return None
    return SQLITE_HDR + dec[:100]


def candidate_ok(candidate: bytes, pages):
    if len(candidate) != 32 or candidate == b"\x00" * 32:
        return None
    salt = pages[0][1][:16]
    configs = [
        ("win3_pbkdf2_4096", hashlib.pbkdf2_hmac("sha1", candidate, salt, 64000, dklen=32), 4096, 48),
        ("raw_4096", candidate, 4096, 48),
        ("raw_1024", candidate, 1024, 48),
        ("pbkdf2_1024", hashlib.pbkdf2_hmac("sha1", candidate, salt, 64000, dklen=32), 1024, 48),
    ]
    for mode, enc_key, page_size, reserve in configs:
        path, page1 = pages[0]
        if not verify_page1(page1, enc_key, page_size, reserve, "sha1"):
            continue
        header = decrypt_page1_header(page1, enc_key, page_size, reserve)
        if not header or not header.startswith(SQLITE_HDR):
            continue
        return {
            "raw_key": candidate.hex(),
            "enc_key": enc_key.hex(),
            "mode": mode,
            "page_size": page_size,
            "reserve": reserve,
            "verified_db": path,
        }
    return None


def find_anchors(handle, module_base: int, module_size: int, regions, max_anchors=40):
    anchors = []
    patterns = [b"iphone\x00", b"android\x00", b"ipad\x00"]
    module_end = module_base + module_size
    for base, size, _protect in regions:
        start = max(base, module_base)
        end = min(base + size, module_end)
        if end <= start:
            continue
        data = read_mem(handle, start, end - start)
        if not data:
            continue
        for pat in patterns:
            pos = 0
            while True:
                idx = data.find(pat, pos)
                if idx < 0:
                    break
                anchors.append((start + idx, pat.decode("ascii", errors="ignore").strip("\x00")))
                pos = idx + 1
                if len(anchors) >= max_anchors:
                    return anchors
    return anchors


def scan_near_anchors(handle, anchors, pages, addr_len: int, max_back=3000):
    seen_ptrs = set()
    attempts = 0
    for anchor_addr, anchor_label in sorted(anchors, reverse=True):
        low = max(0, anchor_addr - max_back)
        for slot in range(anchor_addr, low, -addr_len):
            ptr = read_uint(handle, slot, addr_len)
            if not ptr or ptr in seen_ptrs or ptr < 0x10000 or ptr > 0x7FFFFFFFFFFF:
                continue
            seen_ptrs.add(ptr)
            key_bytes = read_mem(handle, ptr, 32)
            if not key_bytes or len(key_bytes) != 32:
                continue
            attempts += 1
            ok = candidate_ok(key_bytes, pages)
            if ok:
                ok.update(
                    {
                        "anchor": anchor_label,
                        "anchor_addr": hex(anchor_addr),
                        "pointer_slot": hex(slot),
                        "key_addr": hex(ptr),
                        "candidate_attempts": attempts,
                    }
                )
                return ok
    return None


def scan_direct_windows(handle, anchors, pages, max_back=3000):
    attempts = 0
    seen = set()
    for anchor_addr, anchor_label in sorted(anchors, reverse=True):
        data = read_mem(handle, max(0, anchor_addr - max_back), max_back)
        if not data:
            continue
        base = anchor_addr - len(data)
        for off in range(len(data) - 32, -1, -1):
            candidate = data[off : off + 32]
            if candidate in seen:
                continue
            seen.add(candidate)
            attempts += 1
            ok = candidate_ok(candidate, pages)
            if ok:
                ok.update(
                    {
                        "anchor": anchor_label,
                        "anchor_addr": hex(anchor_addr),
                        "direct_addr": hex(base + off),
                        "direct_attempts": attempts,
                    }
                )
                return ok
    return None


def scan_process(pid: int, pages):
    handle = open_process(pid)
    if not handle:
        raise RuntimeError(f"Cannot open WeChat.exe pid={pid}; try running as Administrator")
    try:
        modules = enum_modules(handle)
        wechat_module = next(
            ((base, size, path) for base, size, path in modules if path and path.lower().endswith("wechatwin.dll")),
            None,
        )
        exe_module = next(
            ((base, size, path) for base, size, path in modules if path and path.lower().endswith("wechat.exe")),
            None,
        )
        if not wechat_module:
            raise RuntimeError("WeChatWin.dll is not loaded in this WeChat.exe process")
        module_base, module_size, module_path = wechat_module
        addr_len = get_exe_bit(exe_module[2] if exe_module else module_path) // 8
        regions = enum_regions(handle)
        print(
            f"[module] WeChatWin.dll base={hex(module_base)} size={module_size // 1024 // 1024}MB",
            flush=True,
        )
        anchors = find_anchors(handle, module_base, module_size, regions)
        print(f"[anchors] {len(anchors)} device anchors found", flush=True)
        if not anchors:
            return None
        ok = scan_near_anchors(handle, anchors, pages, addr_len)
        if ok:
            ok.update({"pid": pid, "process": "WeChat.exe", "module": module_path})
            return ok
        ok = scan_direct_windows(handle, anchors, pages)
        if ok:
            ok.update({"pid": pid, "process": "WeChat.exe", "module": module_path})
            return ok
        return None
    finally:
        kernel32.CloseHandle(handle)


def main():
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else find_active_wechat_dir()
    pages = load_target_pages(base)
    print(f"base={base}", flush=True)
    for path, page in pages:
        print(f"target={path} salt={page[:16].hex()} sample={len(page)}", flush=True)
    processes = tasklist_processes()
    print(f"processes={processes}", flush=True)
    if not processes:
        print("[ERROR] WeChat.exe is not running", flush=True)
        return 1
    started = time.time()
    for pid, mem_kb, _name in processes:
        print(f"[scan] pid={pid} mem={mem_kb // 1024}MB", flush=True)
        result = scan_process(pid, pages)
        if not result:
            continue
        result["base_dir"] = str(base)
        result["elapsed_sec"] = round(time.time() - started, 3)
        safe = {k: v for k, v in result.items() if k not in {"raw_key", "enc_key"}}
        print("[FOUND] " + json.dumps(safe, ensure_ascii=False, indent=2), flush=True)
        DEFAULT_OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved={DEFAULT_OUT.resolve()}", flush=True)
        return 0
    print("[MISS] no HMAC-valid key found", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
