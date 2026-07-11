<div align="center">

# Ximalaya-Downloader-Next

**喜马拉雅音频下载器 · 重启版**

![status](https://img.shields.io/badge/status-WIP-orange)
![python](https://img.shields.io/badge/python-3.10+-blue)
![license](https://img.shields.io/badge/license-AGPL--3.0-blue)

</div>

喜马拉雅没有官方下载功能。本工具可把你**有权访问的**内容（免费、已购、会员可听）下载到本地，支持单曲与整张专辑、断点续传、失败重试与优雅停止。

> 项目背景、特性与开发路线图见 [`docs/overview.md`](./docs/overview.md)；架构与设计原则见 [`docs/architecture.md`](./docs/architecture.md)。

## 安装

前置：**Python 3.10+**，并在本机安装 **Google Chrome**（登录与解析都通过接管真实 Chrome 完成）。

```bash
pip install -e .          # 核心库 + CLI
pip install -e '.[tui]'   # 额外装终端面板（TUI）依赖
```

## 登录

首次使用先登录（下载会员/已购内容必须）：

```bash
xdl login
```

会打开一个**专用的 Chrome** 让你完成登录（扫码或账号密码）。登录态持久化在 `~/.xdl/chrome-profile`，之后下载自动复用，无需重复登录。

> 免费的**专辑曲目清单**走公开接口，无需登录即可获取；但逐集下载音频仍需登录（匿名请求会被风控拦截）。

## 命令行

```bash
xdl track <链接或ID>                     # 下载单个音频
xdl album <链接或ID>                     # 下载整张专辑
xdl album <链接或ID> --range 1-20        # 只下第 1–20 集（也支持 5- / -10 / 7）
xdl album <链接或ID> --quality high      # 指定音质：high / standard（默认）/ low
xdl resume                              # 继续上次未完成的下载
xdl risk-report                         # 汇总本地风控观测（不发网络请求）
xdl inspect                             # 诊断：列出 Profile 的设备标识存储 key（不读 value）
```

- **音质**：`--quality` 在可用音质间选择，缺失时自动回退。
- **断点续传**：已存在的文件自动跳过；未下完的 `.part` 会从断点续传。
- **优雅停止**：下载中按 `Ctrl-C` 会存好进度再退出，`xdl resume` 可继续。
- **下载目录**：默认 `./downloads`，可用全局参数 `--download-dir <目录>` 覆盖。
- 任务状态持久化在 `~/.xdl/tasks.db`。
- 受保护接口的最小化观测记录在 `~/.xdl/risk-events.jsonl`；不包含 Cookie、设备指纹或播放 URL。

## 终端面板（TUI）

```bash
xdl-tui
```

一个交互式面板：顶部填链接/ID、选音质与区间，按钮触发下载 / 恢复 / 停止 / 登录；中间是**实时任务表**（每集状态与进度，轮询任务库刷新），底部是日志。适合盯着一批下载的进度。

## 作为 Python 库

核心能力沉淀在可复用的库里，CLI/TUI 只是其上的薄壳。公开入口是 `Facade`（**同步**方法，内部自行驱动异步）：

```python
from xdl import Facade

app = Facade.from_config()
app.download_track("<链接或ID>", quality="standard")
app.download_album("<链接或ID>", quality="standard", range_="1-20")
app.resume()
```

## 关于风控（务必读）

喜马拉雅对**逐集播放信息接口**（`baseInfo`）做了自动化环境风控。本工具当前用 Playwright 通过 CDP 接管真实 Chrome 来获取播放地址，**实测每次会话通常只能下载约 3 集就会被服务端判定为自动化环境并返回 `系统繁忙`，本工具会立即熔断整批、不会持续重试**。在此过程中：

- 你**自己的日常浏览器**用同一账号快速连续播放不会触发风控——这是已知现象，根因在于 CDP 接管本身留下的 inspector 痕迹，无法在 Playwright/CDP 框架内消除。
- 工具会在每次会话启动/登录后**自动重置设备指纹 Cookie 与 localStorage / sessionStorage / IndexedDB**（保留登录态），但这只能换来短暂的"新设备"蜜月，不解决"3 集后又被识别"。用 `xdl inspect` 可看到这些设备标识存储的 key 名（不读 value），用 `xdl risk-report` 可查看历次熔断与恢复时序。
- 下一步治本方向是改用**浏览器扩展 + 本地原生消息**：让 `du_web_sdk` 跑在你日常真实浏览器里，XDL 只被动接收播放地址。

完整的观测记录与差分证据见 [`docs/risk-control-observations.md`](./docs/risk-control-observations.md)。

## 免责声明

本工具仅供个人学习研究。请遵守喜马拉雅的服务条款与相关法律法规，尊重内容创作者的版权，请勿用于侵犯版权或任何商业用途。使用本工具产生的一切后果由使用者自行承担。

## 许可证

[AGPL-3.0](./LICENSE)
