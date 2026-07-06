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
```

- **音质**：`--quality` 在可用音质间选择，缺失时自动回退。
- **断点续传**：已存在的文件自动跳过；未下完的 `.part` 会从断点续传。
- **优雅停止**：下载中按 `Ctrl-C` 会存好进度再退出，`xdl resume` 可继续。
- **下载目录**：默认 `./downloads`，可用全局参数 `--download-dir <目录>` 覆盖。
- 任务状态持久化在 `~/.xdl/tasks.db`。

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

## 免责声明

本工具仅供个人学习研究。请遵守喜马拉雅的服务条款与相关法律法规，尊重内容创作者的版权，请勿用于侵犯版权或任何商业用途。使用本工具产生的一切后果由使用者自行承担。

## 许可证

[AGPL-3.0](./LICENSE)
