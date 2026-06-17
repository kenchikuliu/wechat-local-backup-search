#!/usr/bin/env python3
"""Helpers for local WeChat message knowledge enrichment."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime


ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_@#\.-]{1,31}|\d{4,}")
CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]{2,20}")
SPACE_RE = re.compile(r"\s+")

STOP_TERMS = {
    "http",
    "https",
    "www",
    "com",
    "html",
    "json",
    "tmp",
    "text",
    "type",
    "none",
    "null",
    "image",
    "video",
    "audio",
    "file",
    "link",
    "消息",
    "图片",
    "视频",
    "语音",
    "表情",
    "链接",
    "文件",
    "位置",
    "系统",
    "系统消息",
    "撤回",
    "消息记录",
    "原文",
    "内容",
    "一下",
    "这个",
    "那个",
    "这里",
    "那里",
    "就是",
    "然后",
    "已经",
    "还是",
    "因为",
    "所以",
    "如果",
    "可以",
    "不能",
    "没有",
    "一个",
    "我们",
    "你们",
    "他们",
    "自己",
    "什么",
    "怎么",
    "是否",
    "收到",
    "好的",
    "谢谢",
    "你好",
    "您好",
}


def compact_text(value: str | None) -> str:
    return SPACE_RE.sub(" ", value or "").strip()


def short_snippet(value: str | None, limit: int = 96) -> str:
    text = compact_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def chat_identity(username: str, chat: str) -> str:
    return (username or "").strip() or (chat or "").strip()


def _chinese_terms(span: str) -> list[str]:
    span = span.strip()
    if len(span) < 2:
        return []
    terms: list[str] = []
    if len(span) <= 4:
        terms.append(span)
    for size in (4, 3, 2):
        if len(span) < size:
            continue
        for idx in range(len(span) - size + 1):
            part = span[idx : idx + size]
            if part not in STOP_TERMS:
                terms.append(part)
    return terms


def extract_terms(text: str | None, *, limit: int = 10) -> list[str]:
    raw = compact_text(text)
    if not raw:
        return []

    scores: Counter[str] = Counter()
    for token in ASCII_TOKEN_RE.findall(raw):
        token = token.lower().strip("._-#@")
        if len(token) < 2 or token in STOP_TERMS:
            continue
        scores[token] += 1
    for span in CJK_SPAN_RE.findall(raw):
        for term in _chinese_terms(span):
            if len(term) < 2 or term in STOP_TERMS:
                continue
            scores[term] += 1

    ranked = sorted(scores.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    out: list[str] = []
    for term, _count in ranked:
        if term in out:
            continue
        out.append(term)
        if len(out) >= limit:
            break
    return out


def related_query_terms(text: str | None, *, limit: int = 4) -> list[str]:
    terms = extract_terms(text, limit=16)
    ranked = sorted(terms, key=lambda value: (-len(value), value))
    chosen: list[str] = []
    for term in ranked:
        if any(term in other or other in term for other in chosen):
            continue
        chosen.append(term)
        if len(chosen) >= limit:
            break
    return chosen


def decode_json_list(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


@dataclass
class ChatAccumulator:
    username: str
    chat: str
    is_group: bool
    message_count: int = 0
    text_message_count: int = 0
    first_timestamp: int | None = None
    last_timestamp: int | None = None
    active_days: set[str] = field(default_factory=set)
    sender_counts: Counter[str] = field(default_factory=Counter)
    type_counts: Counter[str] = field(default_factory=Counter)
    keyword_counts: Counter[str] = field(default_factory=Counter)
    keyword_samples: dict[str, str] = field(default_factory=dict)

    def update(
        self,
        *,
        sender: str,
        msg_type: str,
        timestamp: int | None,
        text: str,
    ) -> None:
        self.message_count += 1
        if text:
            self.text_message_count += 1
        if sender:
            self.sender_counts[sender] += 1
        if msg_type:
            self.type_counts[msg_type] += 1
        if timestamp:
            if self.first_timestamp is None or timestamp < self.first_timestamp:
                self.first_timestamp = timestamp
            if self.last_timestamp is None or timestamp > self.last_timestamp:
                self.last_timestamp = timestamp
            try:
                self.active_days.add(datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d"))
            except (OverflowError, OSError, ValueError):
                pass

        seen: set[str] = set()
        for term in extract_terms(text, limit=8):
            if term in seen:
                continue
            seen.add(term)
            self.keyword_counts[term] += 1
            self.keyword_samples.setdefault(term, short_snippet(text))

    def finalize(self) -> dict:
        top_keywords = [
            {
                "term": term,
                "count": count,
                "sample": self.keyword_samples.get(term, ""),
            }
            for term, count in self.keyword_counts.most_common(12)
        ]
        top_types = [
            {"type": msg_type, "count": count}
            for msg_type, count in self.type_counts.most_common(8)
        ]
        top_senders = [
            {"sender": sender, "count": count}
            for sender, count in self.sender_counts.most_common(12)
        ]
        return {
            "username": self.username,
            "chat": self.chat,
            "is_group": int(self.is_group),
            "message_count": self.message_count,
            "text_message_count": self.text_message_count,
            "participant_count": len(self.sender_counts),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "active_days": len(self.active_days),
            "top_sender": top_senders[0]["sender"] if top_senders else "",
            "top_keywords": top_keywords,
            "top_types": top_types,
            "top_senders": top_senders,
        }
