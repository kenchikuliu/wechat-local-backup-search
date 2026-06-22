---
name: wechat-local-backup-search
description: Use when the user wants to build, run, verify, package, troubleshoot, or extend the local Windows WeChat backup-and-search desktop app from the wechat-local-backup-search repository, including background sync, local search index, EXE packaging, and safe handling of local chat backup data.
---

# WeChat Local Backup Search

Use this skill when the task is about the local desktop app that backs up WeChat chats to the local machine and provides fast search over exported messages.

## Repository

- Primary repo path: `C:\tmp\wechat-local-backup-search-publish`
- Remote repo: `https://github.com/kenchikuliu/wechat-local-backup-search.git`
- Built exe: `C:\tmp\wechat-local-backup-search-publish\dist\WeChatBackupSearch.exe`

## Use This Skill For

- Building or rebuilding the desktop app
- Running or validating the local backup/search workflow
- Verifying search index data and backup size
- Troubleshooting WeChat key extraction, decrypt, export, or indexing
- Operating the background sync agent
- Updating customer-facing docs for the desktop product
- Packaging and pushing verified changes to the GitHub repo

## Main Entry Points

- `backup_search_desktop.py`: native desktop UI
- `backup_search_app.py`: backup, indexing, search, and status core
- `backup_search_agent.py`: background sync agent
- `wechat_backup_search_launcher.py`: exe command dispatcher
- `WeChatBackupSearch.spec`: PyInstaller build spec

## Default Workflow

1. Work in `C:\tmp\wechat-local-backup-search-publish`.
2. Read the relevant entrypoint files before changing behavior.
3. For code edits, use `apply_patch`.
4. Validate with `python -m py_compile` before packaging.
5. When real data validation is needed, point `WECHAT_BACKUP_DATA_DIR` at the intended data directory before running checks.
6. Rebuild the exe with:

```powershell
pyinstaller --noconfirm WeChatBackupSearch.spec
```

7. If the build fails because `dist\WeChatBackupSearch.exe` is in use, stop the running `WeChatBackupSearch` process first, then rebuild.

## Common Validation Commands

Syntax check:

```powershell
python -m py_compile backup_search_app.py backup_search_agent.py backup_search_desktop.py wechat_backup_search_launcher.py
```

Search smoke test against an existing backup dataset:

```powershell
$env:WECHAT_BACKUP_DATA_DIR='C:\tmp\wechat-decrypt-inspect\backup_search_data'
python -X utf8 -c "import json,backup_search_app as app; print(json.dumps(app.search_index('合同', limit=3), ensure_ascii=False))"
```

Background agent smoke test:

```powershell
$env:WECHAT_BACKUP_DATA_DIR='C:\tmp\wechat-decrypt-inspect\backup_search_data'
python backup_search_agent.py
```

## Product-Specific Notes

- Keep all chat data local only unless the user explicitly asks otherwise.
- Do not commit decrypted databases, exported chats, local indexes, key files, or logs.
- Prefer verifying behavior with the desktop app or core Python entrypoints instead of guessing.
- The current app supports Windows WeChat 3.x local data flow and a background sync agent.
- The desktop UI includes immediate backup, index rebuild, search, context view, summary view, copy actions, and background sync start/stop.

## When Packaging or Launching

- Desktop app:

```powershell
dist\WeChatBackupSearch.exe
```

- Background agent:

```powershell
dist\WeChatBackupSearch.exe agent
```

## Git Workflow

- Check status before editing.
- Commit only validated changes.
- Push to `origin main` after verification when the user asks to publish.
