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

喜马拉雅对**逐集播放信息接口**（`baseInfo`）存在服务端风控。本工具检测到风险信号会立即熔断整批、不会持续重试。历史观测只能说明特定账号、IP、时段与环境下的现象，不能推出固定的请求次数、恢复时长或通行方案。

- 日常浏览器与 CDP 自动化的差异是一个待观测变量，不应被视为可通过客户端手段消除的固定规则。
- 旧的设备状态重置实验已默认关闭，且登录流程绝不调用它；它既不是登录恢复，也不是风控恢复方案。
- 本项目不提供规避平台反自动化或访问控制的实现。请使用平台提供的 API、下载/导出能力，或取得必要授权。

完整的观测记录与差分证据见 [`docs/risk-control-observations.md`](./docs/risk-control-observations.md)。

### `xm-sign` HTTP 后端（`--source-backend http`，实验性）

该后端为特定 `baseInfo` 请求提供签名字段；它不替代登录 Cookie，也不保证服务端接受请求或免除风控。签名生成仍会访问设备上报服务取得动态字段，因此不是完全离线功能。

- 本地单元测试只验证载荷与响应解析，不等价于真实服务端验收。

  ```bash
  xdl gen-sign -n 3         # 签名连通性探测：会访问上报服务
  ```
- 切到 HTTP 后端前，先运行 `xdl login`。只有在 Chrome 已关闭且专用 Profile 中仍能确认 `1&_token` 时，登录才会成功；无 token 的导出不会覆盖已有 Cookie 缓存：

  ```bash
  xdl login                                            # 一次性，登录态持久化在 ~/.xdl/chrome-profile
  xdl --source-backend http track <链接或ID>            # 单曲
  xdl --source-backend http album <链接或ID>            # 整张专辑
  xdl --source-backend http resume                     # 续传
  xdl refresh-cookies                                  # Cookie 失效后手动再刷一次
  ```
- `xdl gen-sign`、`xdl risk-report` 等诊断命令与音源后端无关，直接运行即可。
- `xm-sign` 最多满足某个请求的签名要求；它不通用于其他接口令牌（例如 `webtk`），也不解决认证、内容授权或服务端风控。
- `chrome`（默认）与 `http` 两条路径共存于装配根（`composition.py`），随时可用
  `--source-backend` 切换；HTTP 路径应只用于获授权的使用场景。

## 免责声明

本工具仅供个人学习研究。请遵守喜马拉雅的服务条款与相关法律法规，尊重内容创作者的版权，请勿用于侵犯版权或任何商业用途。使用本工具产生的一切后果由使用者自行承担。

## 许可证

[AGPL-3.0](./LICENSE)
