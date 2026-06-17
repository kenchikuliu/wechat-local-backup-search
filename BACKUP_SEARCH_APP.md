# 微信本地备份搜索 App

这个 App 是面向普通用户的本地软件壳，复用项目已有的微信数据库解密和导出能力，提供：

- 自动增量备份
- 本机 SQLite 搜索索引
- 浏览器搜索框
- 联系人/群名筛选
- 日期范围筛选
- 本机-only 服务，默认只监听 `127.0.0.1`

数据不会上传服务器。所有备份、索引和日志默认保存在：

```text
backup_search_data/
```

## 开发运行

Windows 建议用管理员终端运行，因为读取微信进程内存需要管理员权限。

```powershell
py -m pip install -r requirements.txt
python backup_search_app.py
```

打开：

```text
http://127.0.0.1:5680
```

首次点击“立即备份”前，请先确认微信已登录并正在运行。

## 打包成 EXE

使用现有打包脚本：

```powershell
build.bat
```

打包后启动备份搜索 App：

```powershell
dist\WeChatDecrypt.exe backup-search
```

如果希望双击 exe 默认进入备份搜索 App，可以把 `wechat_decrypt_launcher.py` 的无参数默认入口从 `monitor_web.py` 改为 `backup_search_app.py`。

## 客户使用流程

1. 启动微信并登录。
2. 以管理员身份运行 `WeChatDecrypt.exe backup-search`。
3. 浏览器打开后点击“立即备份”。
4. 等待备份完成。
5. 在搜索框输入关键词，按联系人/群名或日期筛选。
6. 按需开启“自动备份”，设置备份间隔。

## 安全边界

- HTTP 服务默认绑定 `127.0.0.1:5680`，不是 `0.0.0.0`。
- 服务端会拒绝非本机访问。
- 明文聊天备份和索引都在本机目录中，客户应自行保护电脑账号和磁盘。
- 如开启“备份时转录语音”，是否上传取决于 `config.json` 的转录 backend。默认建议保持本地转录或关闭语音转录。

## 与原 Web UI 的区别

原 `monitor_web.py` 更偏工具箱和实时监听，默认可被局域网访问。

`backup_search_app.py` 更偏客户可用的备份搜索产品：

- 默认本机-only
- 有自动备份设置
- 有持久搜索索引
- 搜索结果不依赖每次实时解密数据库

