#!/usr/bin/env python3
"""Native desktop UI for local WeChat backup/search."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import backup_search_app as core


class BackupSearchDesktop(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("微信本地备份搜索")
        self.geometry("1120x760")
        self.minsize(920, 620)
        self.configure(bg="#f4efe4")
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._result_items: list[dict] = []
        self._build_styles()
        self._build_ui()
        self._load_settings()
        self._refresh_status()
        self._refresh_logs()
        self.after(300, self._poll_queue)
        self.after(3000, self._tick)

    def _build_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f4efe4")
        style.configure("Card.TFrame", background="#fffaf0", relief="flat")
        style.configure("TLabel", background="#f4efe4", foreground="#17211b")
        style.configure("Card.TLabel", background="#fffaf0", foreground="#17211b")
        style.configure("Muted.TLabel", background="#fffaf0", foreground="#67746b")
        style.configure("Title.TLabel", background="#fffaf0", foreground="#17211b", font=("Microsoft YaHei UI", 22, "bold"))
        style.configure("Metric.TLabel", background="#fffaf0", foreground="#1f6f4a", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("Accent.TButton", background="#17211b", foreground="#ffffff")
        style.map("Accent.TButton", background=[("active", "#1f6f4a")])

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, style="Card.TFrame", padding=20)
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="微信本地备份搜索", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="数据只保存在本机。先登录微信，再点击“立即备份”，之后可用关键词快速搜索聊天记录。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))

        metrics = ttk.Frame(header, style="Card.TFrame")
        metrics.grid(row=0, column=1, rowspan=2, sticky="e")
        self.msg_count_var = tk.StringVar(value="0")
        self.chat_count_var = tk.StringVar(value="0")
        self.summary_count_var = tk.StringVar(value="0")
        self.job_var = tk.StringVar(value="空闲")
        self.engine_var = tk.StringVar(value="-")
        self.agent_var = tk.StringVar(value="未启动")
        self._metric(metrics, "已索引消息", self.msg_count_var, 0)
        self._metric(metrics, "会话数量", self.chat_count_var, 1)
        self._metric(metrics, "会话摘要", self.summary_count_var, 2)
        self._metric(metrics, "当前任务", self.job_var, 3)
        self._metric(metrics, "搜索引擎", self.engine_var, 4)
        self._metric(metrics, "后台同步", self.agent_var, 5)

        controls = ttk.Frame(self, style="Card.TFrame", padding=16)
        controls.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        controls.columnconfigure(10, weight=1)
        self.backup_btn = ttk.Button(controls, text="立即备份", style="Accent.TButton", command=self._start_backup)
        self.backup_btn.grid(row=0, column=0, padx=(0, 8))
        self.index_btn = ttk.Button(controls, text="只重建索引", command=self._start_index)
        self.index_btn.grid(row=0, column=1, padx=(0, 14))
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="自动备份", variable=self.auto_var).grid(row=0, column=2, padx=(0, 8))
        ttk.Label(controls, text="间隔").grid(row=0, column=3)
        self.interval_var = tk.StringVar(value="30")
        ttk.Entry(controls, textvariable=self.interval_var, width=6).grid(row=0, column=4, padx=(5, 5))
        ttk.Label(controls, text="分钟").grid(row=0, column=5, padx=(0, 14))
        self.tx_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="备份时转录语音", variable=self.tx_var).grid(row=0, column=6, padx=(0, 10))
        ttk.Button(controls, text="保存设置", command=self._save_settings).grid(row=0, column=7, padx=(0, 10))
        self.agent_btn = ttk.Button(controls, text="启动后台同步", command=self._toggle_agent)
        self.agent_btn.grid(row=0, column=8, padx=(0, 10))
        ttk.Button(controls, text="刷新状态", command=self._refresh_status).grid(row=0, column=9, padx=(0, 10))
        self.last_info_var = tk.StringVar(value="正在读取状态...")
        ttk.Label(controls, textvariable=self.last_info_var).grid(row=1, column=0, columnspan=11, sticky="w", pady=(10, 0))

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))

        search_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        body.add(search_card, weight=3)
        search_card.columnconfigure(0, weight=1)
        search_card.rowconfigure(2, weight=1)

        search_form = ttk.Frame(search_card, style="Card.TFrame")
        search_form.grid(row=0, column=0, sticky="ew")
        search_form.columnconfigure(0, weight=2)
        search_form.columnconfigure(1, weight=1)
        self.query_var = tk.StringVar()
        self.chat_var = tk.StringVar()
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        ttk.Entry(search_form, textvariable=self.query_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Entry(search_form, textvariable=self.chat_var).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Entry(search_form, textvariable=self.start_var, width=12).grid(row=0, column=2, padx=(0, 8))
        ttk.Entry(search_form, textvariable=self.end_var, width=12).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(search_form, text="搜索", style="Accent.TButton", command=self._search).grid(row=0, column=4)
        ttk.Label(
            search_card,
            text="关键词 | 联系人/群名 | 起始日期 YYYY-MM-DD | 结束日期 YYYY-MM-DD",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(8, 8))

        columns = ("time", "chat", "sender", "type", "content")
        self.tree = ttk.Treeview(search_card, columns=columns, show="headings", height=18)
        self.tree.heading("time", text="时间")
        self.tree.heading("chat", text="联系人/群")
        self.tree.heading("sender", text="发送者")
        self.tree.heading("type", text="类型")
        self.tree.heading("content", text="内容")
        self.tree.column("time", width=145, anchor="w")
        self.tree.column("chat", width=160, anchor="w")
        self.tree.column("sender", width=110, anchor="w")
        self.tree.column("type", width=65, anchor="w")
        self.tree.column("content", width=420, anchor="w")
        self.tree.grid(row=2, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_result)
        yscroll = ttk.Scrollbar(search_card, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=2, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=yscroll.set)

        detail_frame = ttk.Frame(search_card, style="Card.TFrame")
        detail_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        detail_frame.columnconfigure(0, weight=1)
        ttk.Label(detail_frame, text="消息详情", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        detail_toolbar = ttk.Frame(detail_frame, style="Card.TFrame")
        detail_toolbar.grid(row=0, column=1, sticky="e")
        ttk.Button(detail_toolbar, text="复制详情", command=self._copy_detail).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(detail_toolbar, text="复制摘要", command=self._copy_summary).grid(row=0, column=1)
        self.detail_text = tk.Text(detail_frame, height=8, wrap="word", bg="#fffef8", relief="flat")
        self.detail_text.grid(row=1, column=0, sticky="nsew", pady=(5, 0))

        insight_frame = ttk.Frame(search_card, style="Card.TFrame")
        insight_frame.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        insight_frame.columnconfigure(0, weight=1)
        insight_frame.columnconfigure(1, weight=1)
        ttk.Label(insight_frame, text="会话摘要", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(insight_frame, text="相关会话 / 关键词", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.summary_text = tk.Text(insight_frame, height=11, wrap="word", bg="#fffef8", relief="flat")
        self.summary_text.grid(row=1, column=0, sticky="nsew", pady=(5, 0), padx=(0, 6))
        self.related_text = tk.Text(insight_frame, height=11, wrap="word", bg="#fffef8", relief="flat")
        self.related_text.grid(row=1, column=1, sticky="nsew", pady=(5, 0), padx=(6, 0))

        log_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        body.add(log_card, weight=1)
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        log_head = ttk.Frame(log_card, style="Card.TFrame")
        log_head.grid(row=0, column=0, sticky="ew")
        log_head.columnconfigure(0, weight=1)
        ttk.Label(log_head, text="运行日志", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(log_head, text="刷新", command=self._refresh_logs).grid(row=0, column=1)
        self.log_text = tk.Text(log_card, width=38, wrap="none", bg="#141914", fg="#d9eadc", relief="flat")
        self.log_text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        self.bind("<Return>", lambda _e: self._search())

    def _metric(self, parent, label, var, col):
        box = ttk.Frame(parent, style="Card.TFrame", padding=(12, 0))
        box.grid(row=0, column=col, sticky="n", padx=4)
        ttk.Label(box, textvariable=var, style="Metric.TLabel").grid(row=0, column=0)
        ttk.Label(box, text=label, style="Muted.TLabel").grid(row=1, column=0)

    def _load_settings(self):
        status = core.app_status()
        settings = status["settings"]
        self.auto_var.set(bool(settings.get("auto_backup_enabled")))
        self.tx_var.set(bool(settings.get("with_transcriptions")))
        self.interval_var.set(str(settings.get("backup_interval_minutes") or 30))

    def _tick(self):
        self._refresh_status()
        self.after(3000, self._tick)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "backup_done":
                    self._refresh_status()
                    self._refresh_logs()
                    if payload.get("ok"):
                        messagebox.showinfo("备份完成", payload.get("summary", "备份完成"))
                    else:
                        messagebox.showerror("备份失败", payload.get("error", "备份失败"))
                elif kind == "index_done":
                    self._refresh_status()
                    self._refresh_logs()
                    if payload.get("ok"):
                        messagebox.showinfo("索引完成", payload.get("summary", "索引完成"))
                    else:
                        messagebox.showerror("索引失败", payload.get("error", "索引失败"))
        except queue.Empty:
            pass
        self.after(300, self._poll_queue)

    def _refresh_status(self):
        status = core.app_status()
        idx = status["index"]
        job = status["job"]
        settings = status["settings"]
        agent = status.get("background_agent") or {}
        self.msg_count_var.set(str(idx.get("message_count", 0)))
        self.chat_count_var.set(str(idx.get("chat_count", 0)))
        self.summary_count_var.set(str(idx.get("summary_chat_count", 0)))
        self.engine_var.set(idx.get("fts_tokenizer") or "LIKE")
        self.job_var.set(job.get("step") if job.get("running") else ("失败" if job.get("ok") is False else "空闲"))
        running_agent = bool(agent.get("running"))
        self.agent_var.set("运行中" if running_agent else "未启动")
        self.agent_btn.configure(text="停止后台同步" if running_agent else "启动后台同步")
        running = bool(job.get("running"))
        self.backup_btn.configure(state="disabled" if running else "normal")
        self.index_btn.configure(state="disabled" if running else "normal")
        last = settings.get("last_backup_at") or "从未备份"
        indexed = settings.get("last_indexed_at") or idx.get("last_indexed_at") or "从未索引"
        agent_msg = ""
        if running_agent:
            agent_msg = f"；后台同步 PID：{agent.get('pid', '')}"
        msg = job.get("message") or settings.get("last_error") or settings.get("last_backup_summary") or ""
        msg = (msg or "") + agent_msg
        self.last_info_var.set(f"上次备份：{last}；上次索引：{indexed}；{msg}")

    def _refresh_logs(self):
        logs = core._latest_logs()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", logs or "暂无日志")
        self.log_text.configure(state="disabled")

    def _save_settings(self):
        try:
            interval = int(self.interval_var.get() or "30")
        except ValueError:
            messagebox.showerror("设置错误", "备份间隔必须是数字")
            return
        core._update_settings(
            auto_backup_enabled=self.auto_var.get(),
            backup_interval_minutes=interval,
            with_transcriptions=self.tx_var.get(),
        )
        self._refresh_status()
        messagebox.showinfo("设置已保存", "自动备份设置已保存")

    def _toggle_agent(self):
        status = core.app_status()
        agent = status.get("background_agent") or {}
        if agent.get("running"):
            self._stop_agent(agent)
        else:
            self._start_agent()

    def _start_agent(self):
        exe = sys.executable if getattr(sys, "frozen", False) else None
        if exe and exe.lower().endswith(".exe"):
            subprocess.Popen([exe, "agent"], cwd=core.APP_DIR)
        else:
            subprocess.Popen(
                [sys.executable, core.os.path.join(core.SCRIPT_DIR, "backup_search_agent.py")],
                cwd=core.APP_DIR,
            )
        self.after(1500, self._refresh_status)

    def _stop_agent(self, agent: dict):
        pid = int(agent.get("pid") or 0)
        if pid <= 0:
            return
        try:
            if core.os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
            else:
                core.os.kill(pid, 15)
        except Exception as exc:
            messagebox.showerror("停止失败", f"无法停止后台同步：{exc}")
            return
        self.after(1500, self._refresh_status)

    def _start_backup(self):
        if core.job_state.get("running"):
            messagebox.showwarning("任务运行中", "已有任务正在运行")
            return
        self._refresh_status()
        t = threading.Thread(target=self._run_backup_worker, daemon=True)
        t.start()

    def _run_backup_worker(self):
        result = core.run_backup(manual=True)
        self._queue.put(("backup_done", result))

    def _start_index(self):
        if core.job_state.get("running"):
            messagebox.showwarning("任务运行中", "已有任务正在运行")
            return
        self._refresh_status()
        t = threading.Thread(target=self._run_index_worker, daemon=True)
        t.start()

    def _run_index_worker(self):
        result = core.run_index_only()
        self._queue.put(("index_done", result))

    def _search(self):
        result = core.search_index(
            self.query_var.get(),
            chat=self.chat_var.get(),
            start=self.start_var.get(),
            end=self.end_var.get(),
            limit=100,
        )
        if result.get("error"):
            messagebox.showwarning("搜索不可用", result["error"])
            return
        self._result_items = result.get("items", [])
        for item in self.tree.get_children():
            self.tree.delete(item)
        for idx, item in enumerate(self._result_items):
            content = (item.get("content") or "").replace("\n", " ")
            if len(content) > 160:
                content = content[:160] + "..."
            self.tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(
                    item.get("datetime", ""),
                    item.get("chat") or item.get("username") or "",
                    item.get("sender", ""),
                    item.get("type", "text"),
                    content,
                ),
            )
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", f"找到 {result.get('total', 0)} 条，显示 {len(self._result_items)} 条。")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", "选择一条消息查看会话摘要和前后文。")
        self.related_text.delete("1.0", "end")
        related = result.get("related_chats") or []
        if related:
            lines = []
            for item in related[:12]:
                lines.append(
                    f"{item.get('chat') or item.get('username') or ''}\n"
                    f"关键词：{item.get('term', '')}  命中：{item.get('count', 0)}\n"
                    f"{item.get('sample', '')}"
                )
            self.related_text.insert("1.0", "\n\n".join(lines))
        else:
            self.related_text.insert("1.0", "暂无相关会话关键词。")

    def _show_selected_result(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._result_items):
            return
        item = self._result_items[idx]
        text = (
            f"时间：{item.get('datetime', '')}\n"
            f"会话：{item.get('chat') or item.get('username') or ''}\n"
            f"发送者：{item.get('sender', '')}\n"
            f"类型：{item.get('type', 'text')}\n"
            f"来源文件：{item.get('source_file', '')}\n\n"
            f"{item.get('content', '')}"
        )
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        summary = item.get("chat_summary") or {}
        keywords = summary.get("top_keywords") or []
        top_senders = summary.get("top_senders") or []
        top_types = summary.get("top_types") or []
        context = item.get("context") or []
        summary_lines = [
            f"消息总数：{summary.get('message_count', 0)}",
            f"文本消息：{summary.get('text_message_count', 0)}",
            f"活跃天数：{summary.get('active_days', 0)}",
            f"参与者：{summary.get('participant_count', 0)}",
        ]
        if summary.get("top_sender"):
            summary_lines.append(f"最活跃发送者：{summary.get('top_sender')}")
        if keywords:
            summary_lines.append(
                "高频关键词：" + " / ".join(
                    f"{entry.get('term')}({entry.get('count')})" for entry in keywords[:8]
                )
            )
        if top_senders:
            summary_lines.append(
                "发送者分布：" + " / ".join(
                    f"{entry.get('sender')}({entry.get('count')})" for entry in top_senders[:6]
                )
            )
        if top_types:
            summary_lines.append(
                "消息类型：" + " / ".join(
                    f"{entry.get('type')}({entry.get('count')})" for entry in top_types[:6]
                )
            )
        if context:
            summary_lines.append("")
            summary_lines.append("前后文：")
            for ctx in context:
                summary_lines.append(
                    f"{ctx.get('datetime', '')} {ctx.get('sender', '')} [{ctx.get('type', '')}] {ctx.get('content', '')}"
                )
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", "\n".join(summary_lines) if summary_lines else "暂无会话摘要。")

    def _copy_text(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()

    def _copy_detail(self):
        text = self.detail_text.get("1.0", "end").strip()
        if text:
            self._copy_text(text)
            messagebox.showinfo("已复制", "消息详情已复制到剪贴板")

    def _copy_summary(self):
        text = self.summary_text.get("1.0", "end").strip()
        if text:
            self._copy_text(text)
            messagebox.showinfo("已复制", "会话摘要已复制到剪贴板")


def main():
    core._ensure_dirs()
    core._append_log("desktop app started")
    scheduler = threading.Thread(target=core.scheduler_loop, daemon=True)
    scheduler.start()
    app = BackupSearchDesktop()
    try:
        app.mainloop()
    finally:
        core.scheduler_stop.set()


if __name__ == "__main__":
    main()
