<div align="center">

# Ximalaya-Downloader-Next

**喜马拉雅音频下载器 · 重启版**

![status](https://img.shields.io/badge/status-WIP-orange)
![python](https://img.shields.io/badge/python-3.10+-blue)
![license](https://img.shields.io/badge/license-AGPL--3.0-blue)

</div>

下载你有权访问的喜马拉雅内容，支持单曲、专辑、断点续传、失败重试与任务恢复。

当前默认链路使用纯 Python 在本地生成 `xm-sign`，再通过 HTTP 请求播放信息。Google Chrome 或 Microsoft Edge 用于交互登录，以及 Cookie 缓存失效时从专用 Profile 读取已持久化会话，不负责默认下载请求。

> `xm-sign` 只满足特定接口的签名要求，不能替代登录、内容授权，也不保证服务端一定接受请求。请只下载你有权访问的内容。

## 快速开始

要求：Python 3.10+、Google Chrome 或 Microsoft Edge。

```bash
pip install -e .
```

启动本地 WebUI：

```bash
xdl web
# 或：xdl-web
```

程序会自动打开 `http://127.0.0.1:8787`。在页面顶部点击“尚未登录”即可完成首次登录；随后可在同一个界面新建单曲/专辑下载、选择音质与区间、恢复任务、查看本地风控报告并调整运行设置。

也可以继续使用 CLI。首次使用先登录：

```bash
xdl login
```

浏览器打开后完成登录，并按终端提示确认。程序会在活动登录上下文中捕获 Cookie、验证登录态确实已写入专用 Profile，再保存下载所需的 Cookie；不会为了导出而重启浏览器，成功后无需再执行刷新命令。

### 浏览器选择

默认自动探测：Chrome 优先，未安装 Chrome 时使用 Edge，无需任何配置。两个浏览器都装了、又想指定其一，可加全局参数：

```bash
xdl --browser edge login
xdl --browser edge track <链接或ID>
```

每个浏览器的登录态、Cookie 缓存与设备指纹各自独立保存（`~/.xdl/{chrome,edge}-profile`、`{chrome,edge}-cookies.json`、`{chrome,edge}-device-info.json`），互不覆盖。切换浏览器需要在新浏览器中重新登录一次，原浏览器的登录态会完整保留，切回即可恢复。WebUI 用户直接在设置页的「路径与浏览器」中选择即可。

随后直接下载：

```bash
xdl track <音频链接或 trackId>
xdl album <专辑链接或 albumId>
```

## 常用命令

```bash
xdl web                                # 启动本地 WebUI
xdl login                              # 首次登录或重新登录
xdl track <链接或ID>                    # 下载单个音频
xdl track -F <链接或ID>                 # 列出所有可用音质格式，不下载
xdl album <链接或ID>                    # 下载整张专辑
xdl album <链接或ID> --range 1-20       # 只下载指定区间
xdl album <链接或ID> --quality high     # high / standard / low
xdl --concurrency 3 album <链接或ID>    # 自定义异步并发数（默认 1）
xdl --risk-poll album <链接或ID>        # 风控后自动轮询恢复并继续下载
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

WebUI 默认只监听本机回环地址，没有远程访问认证。不要把它直接暴露到公网；确需修改监听地址时可使用 `xdl web --host <地址> --port <端口>`。

### 下载行为

- `xdl track -F <链接或ID>` 会按码率和编码优先级列出可用格式，只读取播放元数据，不下载音频。
- 音质缺失时会自动回退到可用规格。
- 已存在的完整文件会跳过。
- 未完成的 `.part` 文件支持 HTTP Range 续传。
- 下载中按 `Ctrl-C` 会保存进度并优雅退出，之后运行 `xdl resume`。
- 专辑下载和恢复默认使用 `1` 个异步 worker；可用全局参数 `--concurrency N` 调整。
- 提高并发会同时增加播放信息请求和媒体下载数量，可能更容易触发平台风控；遇到已识别的风控信号会停止整批，同一批次只提示一次，其余项目保留待恢复。
- 风控自动恢复（默认关闭）：加 `--risk-poll` 后，遇风控且任务未完成时自动等待并探测，风控解除后按原并发继续下载，等待期间不会反复请求；超时后回落为熔断留待 `xdl resume`。
- 自定义等待参数：`--risk-poll-initial-wait`（首次等待，默认 30s）、`--risk-poll-max-duration`（总等待上限，默认 3600s，0=不限），例如 `xdl --risk-poll --risk-poll-initial-wait 60 --risk-poll-max-duration 1800 album <链接或ID>`；WebUI 在「风控自动恢复」设置区可配置。

## 默认 HTTP 后端

默认的 `http` 后端按下面的顺序工作：

1. 从本地 Cookie 缓存读取已验证的登录态；缓存过期时才从专用浏览器 Profile 重新导出。
2. `PySignProvider` 读取内置设备信息模板或用户配置，并向设备上报服务取得本次 `cadd` 与 `sid`。
3. 组合 `xm-sign`、Cookie 和必要请求头，调用 `baseInfo`。
4. 解码播放地址并交给下载任务引擎落盘。

可用以下命令只检查签名生成，不访问受保护的播放信息接口：

```bash
xdl gen-sign
xdl gen-sign -n 3
```

该命令仍会访问设备上报服务，因此不是完全离线操作。

### 设备信息（可选）

`xdl login` 会在该浏览器首次登录后自动采集一次设备信息，日常下载一般不需要手动维护；采集失败时会退回包内模板并给出提示。需要自检或更新时：

```bash
xdl extract-device                 # 从专用 Profile 采集到 ~/.xdl/{browser}-device-info.json
xdl extract-device -o <路径>       # 指定输出路径
xdl gen-sign                       # 检查本地签名链路
xdl gen-sign --device-info <路径>  # 使用指定设备信息文件
```

另有默认关闭的实验开关 `--experiment-rotate-device`：在识别到风控后可尝试刷新设备信息并重试当前曲。**不保证**恢复可用，也不构成对平台访问控制的绕过；细项通过 `Settings` 配置。

当前换身默认更偏向“成套新身份”：

- 临时全新 Profile + 只播种登录 Cookie
- 有头采集，多轮清 storage 后重生
- **保留**浏览器导出的设备 Cookie（与新 `device_info` 成套；可用 `--experiment-strip-device-cookies` 改回剥离）
- 上报前去掉 collector 内部字段；若 session/hardware 身份字段完全没变则中止假换身

### 浏览器 CDP 兼容后端

旧的浏览器/CDP 音源仍作为兼容路径保留，但不推荐日常使用：

```bash
xdl --source-backend chrome track <链接或ID>
```

该路径主要用于登录与兼容诊断，接管哪个浏览器跟随 `--browser` 设置。只有在默认 HTTP 后端暂时不兼容且你理解其限制时才使用它。

## PC 桌面端后端

面向桌面客户端场景的轻量后端，纯 HTTP，无需浏览器参与：

```bash
xdl --source-backend pc track <链接或ID>
xdl --source-backend pc album <链接或ID>
```

登录方式与默认后端一致：先 `xdl login` 保存会话，之后直接下载即可。日常批量下载时解析更稳定，WebUI 也可在设置页「音源后端」中切换。

## Android APK 协议后端

APK 9.5.1.3 协议作为独立可选后端提供，不读取或修改 WEB/PC 后端的浏览器 Profile、Cookie、签名或下载逻辑：

```bash
xdl --source-backend apk web
```

在 WebUI 设置中把「音源后端」切换为「Android APK 协议」，保存后点击顶部登录状态，完成 GeeTest 4 与短信验证码登录。APK 身份独立保存在 `~/.xdl/apk/`。

APK 整专下载会先取得授权曲目清单，然后对每一集分别生成 `x-tk`、请求一次最新下载连接并立即下载。连接不会写入 SQLite 或跨集缓存；`.part` 恢复时也会重新获取该集连接。媒体连接返回 `401/403/404` 时会作废旧连接并最多刷新一次。

该后端需要 Java 17+，随附的版本绑定 native bundle 位于 `vendor/apk_protocol/`，启动时会按 `manifest.json` 校验 SHA-256。可以通过 `Settings.apk_*` 字段覆盖 Java、JAR、`.so`、asset 与状态路径。

## 本地数据

默认用户数据位于 `~/.xdl`：

| 路径 | 用途 |
|---|---|
| `chrome-profile/` | 专用 Chrome 登录会话 |
| `chrome-cookies.json` | Chrome 身份的登录 Cookie 缓存，属于敏感数据 |
| `chrome-device-info.json` | Chrome 身份的设备信息；不存在时使用包内模板 |
| `edge-profile/` `edge-cookies.json` `edge-device-info.json` | 同上，Edge 身份（选用 Edge 时） |
| `tasks.db` | 下载任务、进度和恢复状态 |
| `risk-events.jsonl` | 最小化请求结果观测（供 `risk-report`），不含 Cookie 或播放 URL |
| `apk/device.json` `apk/xuid.json` `apk/auth.json` | APK 独立设备身份与登录态；`auth.json` 权限为 `0600` |

Profile、Cookie 缓存与设备信息共同构成**一份身份**，三者必须同源于同一个浏览器，因此统一按浏览器分文件保存。从旧版本升级时，`cookies.json` 与 `device-info.json` 会在首次运行时自动改名为 `chrome-*`，登录态不受影响。

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
pip install -e '.[dev]'
python -m pytest -q
python -m compileall -q src tests
```

测试默认使用替身，不会访问真实登录态、设备上报服务或播放信息接口。离线测试通过不等于真实平台验收通过。

更多文档：

- [项目现状与范围](./docs/overview.md)
- [架构设计](./docs/architecture.md)
- [WebUI 使用与接口](./docs/webui.md)

## 免责声明与许可证

本项目仅供学习研究。请遵守平台服务条款和相关法律法规，尊重内容创作者版权，勿用于侵权或商业用途。使用本工具产生的后果由使用者自行承担。

[AGPL-3.0](./LICENSE)
