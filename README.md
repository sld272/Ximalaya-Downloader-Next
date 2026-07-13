<div align="center">

# Ximalaya-Downloader-Next

**喜马拉雅音频下载器 · 重启版**

![status](https://img.shields.io/badge/status-WIP-orange)
![python](https://img.shields.io/badge/python-3.10+-blue)
![license](https://img.shields.io/badge/license-AGPL--3.0-blue)

</div>

下载你有权访问的喜马拉雅内容，支持单曲、专辑、断点续传、失败重试与任务恢复。

当前默认链路使用纯 Python 在本地生成 `xm-sign`，再通过 HTTP 请求播放信息。Google Chrome 用于交互登录，以及 Cookie 缓存失效时从专用 Profile 读取已持久化会话，不负责默认下载请求。

> `xm-sign` 只满足特定接口的签名要求，不能替代登录、内容授权，也不保证服务端一定接受请求。请只下载你有权访问的内容。

## 快速开始

要求：Python 3.10+、Google Chrome。

```bash
pip install -e .
```

首次使用先登录：

```bash
xdl login
```

浏览器打开后完成登录，并按终端提示确认。程序会验证登录态确实已写入专用 Profile，然后自动导出下载所需的 Cookie；成功后无需再执行刷新命令。

随后直接下载：

```bash
xdl track <音频链接或 trackId>
xdl album <专辑链接或 albumId>
```

## 常用命令

```bash
xdl login                              # 首次登录或重新登录
xdl track <链接或ID>                    # 下载单个音频
xdl album <链接或ID>                    # 下载整张专辑
xdl album <链接或ID> --range 1-20       # 只下载指定区间
xdl album <链接或ID> --quality high     # high / standard / low
xdl --concurrency 3 album <链接或ID>     # 自定义异步并发数（默认 1）
xdl resume                             # 恢复未完成任务
xdl gen-sign                           # 检查本地签名链路
xdl risk-report                        # 汇总本地风控记录，不发网络请求
```

全局选项必须写在子命令之前：

```bash
xdl --download-dir D:\Audio album <链接或ID>
xdl --concurrency 3 resume
```

默认下载目录为当前目录下的 `downloads`。

### 下载行为

- 音质缺失时会自动回退到可用规格。
- 已存在的完整文件会跳过。
- 未完成的 `.part` 文件支持 HTTP Range 续传。
- 下载中按 `Ctrl-C` 会保存进度并优雅退出，之后运行 `xdl resume`。
- 专辑下载和恢复默认使用 `1` 个异步 worker；可用全局参数 `--concurrency N` 调整。
- 提高并发会同时增加播放信息请求和媒体下载数量，可能更容易触发平台风控；遇到已识别的风控信号仍会停止整批。

## 默认 HTTP 后端

默认的 `http` 后端按下面的顺序工作：

1. 从本地 Cookie 缓存读取已验证的登录态；缓存过期时才从专用 Chrome Profile 重新导出。
2. `PySignProvider` 读取内置设备信息模板或用户配置，并向设备上报服务取得本次 `cadd` 与 `sid`。
3. 组合 `xm-sign`、Cookie 和必要请求头，调用 `baseInfo`。
4. 解码播放地址并交给下载任务引擎落盘。

可用以下命令只检查签名生成，不访问受保护的播放信息接口：

```bash
xdl gen-sign
xdl gen-sign -n 3
```

该命令仍会访问设备上报服务，因此不是完全离线操作。

### Chrome 兼容后端

旧的 Chrome/CDP 音源仍作为兼容路径保留，但不推荐日常使用：

```bash
xdl --source-backend chrome track <链接或ID>
```

历史实测表明 CDP 环境可能更容易触发验证码或 `1001` / `3005` 风控。只有在默认 HTTP 后端暂时不兼容且你理解这一限制时才使用它。

## 终端面板

安装可选依赖：

```bash
pip install -e '.[tui]'
xdl-tui
```

终端面板提供链接输入、音质与区间选择、下载/恢复/停止/登录按钮，以及从任务库刷新的逐集状态表。它与 CLI 使用同一个默认 HTTP 后端。

## 本地数据

默认用户数据位于 `~/.xdl`：

| 路径 | 用途 |
|---|---|
| `chrome-profile/` | 专用 Chrome 登录会话 |
| `cookies.json` | HTTP 后端使用的登录 Cookie 缓存，属于敏感数据 |
| `device-info.json` | 可选设备信息；不存在时使用包内模板 |
| `tasks.db` | 下载任务、进度和恢复状态 |
| `risk-events.jsonl` | 最小化风控观测，不含 Cookie 或播放 URL |

可通过环境变量 `XDL_HOME` 修改用户数据根目录。

## Python API

```python
from xdl import Facade

app = Facade.from_config()
app.download_track("<链接或ID>", quality="standard")
app.download_album("<链接或ID>", quality="standard", range_="1-20")
app.resume()
```

`Facade` 提供同步接口，内部负责异步音源与任务生命周期。

## 开发与验证

```bash
pip install -e '.[dev,tui]'
python -m pytest -q
python -m compileall -q src tests
```

测试默认使用替身，不会访问真实登录态、设备上报服务或播放信息接口。离线测试通过不等于真实平台验收通过。

更多文档：

- [项目现状与范围](./docs/overview.md)
- [架构设计](./docs/architecture.md)

## 免责声明与许可证

本项目仅供学习研究。请遵守平台服务条款和相关法律法规，尊重内容创作者版权，勿用于侵权或商业用途。使用本工具产生的后果由使用者自行承担。

[AGPL-3.0](./LICENSE)
