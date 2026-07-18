# 架构设计

本文描述当前 checkout 已实现的结构，不把规划中的模块写成现有能力。

## 1. 架构目标

项目采用端口与适配器结构，外加可恢复任务引擎。核心目标是把平台易变部分——签名、Cookie、接口响应和媒体地址解码——限制在适配器层，使下载编排、任务状态和前端保持稳定。

依赖方向：

```text
CLI / TUI / Python 调用方
          │
          ▼
       Facade
          │
          ▼
应用用例与任务调度 ──────→ 领域模型
          │
          ▼
        端口
          ▲
          │ 实现
HTTP/Chrome 音源、签名、解码、文件、SQLite 适配器
```

领域层不做 I/O；应用层只依赖领域对象和端口；装配根负责选择具体适配器。

## 2. 模块边界

| 模块 | 职责 | 不应承担 |
|---|---|---|
| `domain` | 数据模型、状态、音质协商、ID/区间解析、命名 | 网络、磁盘、进程管理 |
| `ports` | 描述应用层依赖的外部能力 | 平台响应细节 |
| `application` | 下载、专辑、恢复、重试和停止编排 | 构造 HTTP 请求或 SQL |
| `adapters` | 平台请求、签名、解码、下载落盘、SQLite | 用户交互流程编排 |
| `frontends` | 参数/输入、进度和结果展示 | 复制业务规则 |
| `composition.py` | 根据 `Settings` 组装对象图 | 下载业务逻辑 |
| `config` | 平台常量、签名常量、用户数据路径 | 运行期任务状态 |

`Facade` 是公开同步边界。它用 `asyncio.run` 驱动异步 `Source` 生命周期，使 CLI、TUI 和普通脚本不需要自行管理事件循环。

## 3. 默认下载链路

`Settings.source_backend` 默认是 `http`。

### 3.1 登录态

`xdl login` 通过 `ChromeSource.interactive_login()` 打开专用 Chrome。验证分两步：

1. 在活动浏览器上下文中确认存在非空的 `*&_token` Cookie，并在关闭前捕获全部目标域 Cookie。
2. 正常关闭 Chrome 后，以只读方式检查 Cookie 数据库，确认 token 已写入磁盘。

`HttpSource.interactive_login()` 直接接收活动登录上下文捕获的 Cookie，并只在确认登录 token 存在时原子更新 `~/.xdl/cookies.json`。登录后不会为了导出 Cookie 再次启动 Profile，避免会话 Cookie 跨重启丢失，也避免 Playwright 的测试凭据存储参数与系统 Chrome 的 Cookie 加密密钥不一致。匿名结果不会覆盖已有的有效缓存。

### 3.2 `xm-sign`

`PySignProvider` 实现 `SignProvider`：

```text
device_info
  → 紧凑 JSON
  → URI 字节编码
  → zlib
  → AES-ECB + PKCS#7
  → POST 设备上报服务
  → Base64 解码 + AES 解密
  → 同一次响应中的 cadd && sid
```

每次 `sign()` 进行一次设备上报，并使用该响应成对返回的 `cadd` 与 `sid`。旧的 `cadd` 缓存没有减少上报次数，还可能组合不匹配的数据，已移除内部缓存逻辑；兼容构造参数仍暂时保留。

设备信息优先读取 `~/.xdl/device-info.json`，文件不存在或不可读时使用包内 `device_info_default.json`。`Zf5` 在每次上报前更新为当前毫秒时间戳。加载设备信息时会对明显异常的 UA 字段做规范化，使上报载荷与常规浏览器形态一致；这不替代登录或内容授权。

### 3.3 HTTP 音源

`HttpSource.open()` 优先读取新鲜且包含登录 token 的 Cookie 缓存。只有缓存缺失、过期或匿名时才启动专用 Profile 重新导出，因此已有有效缓存时不依赖 Profile 目录仍然存在。重新导出会保留系统 Chrome 的密码存储参数，不使用 Playwright 的 mock keychain。

单曲请求流程：

1. 生成本次 `xm-sign`。
2. 构造带当前毫秒时间戳的 `/mobile-playpage/track/v3/baseInfo/{ts}`。
3. 附加 Cookie、Origin、Referer 和必要请求头。
4. 通过 `curl-cffi` 发送；显式关闭 impersonation 时回退到 `requests`。
5. 分类 `ret/msg`，解析播放规格并交给 `Decoder`。

专辑曲目清单由 `_album_list.py` 调用公开的 `/revision/album/getTracksList`，不需要 `xm-sign`。

可选实验功能：`Settings.experiment_rotate_device_on_risk`（CLI：`--experiment-rotate-device`）开启时，在识别到风控后可刷新本地设备信息并重试当前曲。默认关闭；其余参数见 `Settings`。该能力不保证恢复可用，也不构成对平台访问控制的绕过。

### 3.4 Chrome 兼容音源

`--source-backend chrome` 选择 `ChromeSource`。它启动 Chrome 后通过 CDP 连接，以只读网络响应监听取得目标 `baseInfo`；页面没有自行请求时会点击播放控件。

该路径保留登录、诊断和兼容价值，但历史观测表明 CDP 环境可能被平台识别，因此不是默认下载实现。旧的设备状态重置实验默认关闭，也不在登录流程执行。

## 4. 任务与恢复

`DownloadTrackUseCase`、`DownloadAlbumUseCase` 和 `ResumeUseCase` 负责：

- 打开/关闭一次音源会话。
- 将专辑曲目写入 SQLite 任务库。
- 按 `max_concurrency` 有界调度；默认值为 `1`，CLI 可通过全局参数
  `--concurrency N` 覆盖。
- 将可重试失败按类型退避，将风控错误升级为批次熔断；风控作为单个批次事件汇总一次，其余受影响任务保留待恢复。
- 传播用户停止信号并保留未完成任务。

`SqliteTaskStore` 保存任务状态、错误、字节进度和专辑元数据。`FileSink` 使用 `.part` 文件下载；服务器支持 Range 且校验一致时续传，完成后原子替换最终文件。

## 5. 错误模型

所有预期业务失败继承 `XdlError`：

| 类型 | 含义 | 默认策略 |
|---|---|---|
| `ConfigError` | 本地配置或依赖不满足 | 立即失败并给出操作提示 |
| `AuthError` | 未登录、过期或无权访问 | 不盲目重试 |
| `SignError` | 本地载荷或上报失败 | 受限退避重试 |
| `NetworkError` | 超时、连接失败 | 受限退避重试 |
| `ApiError` | 平台响应或数据形态异常 | 按 `retryable` 决定 |
| `RiskControlError` | 已识别的频控/验证码信号 | 熔断当前批次 |
| `DecodeError` | 播放地址解码失败 | 记录失败 |
| `StorageError` | 文件或 SQLite 失败 | 停止相关操作 |
| `CancelledByUser` | 用户请求优雅停止 | 保存进度并返回 130 |

`3005` 是复用码，只有消息语义为“系统繁忙”时才按风控处理；其他 `3005` 仍按鉴权/权限错误处理。匿名 `1001` 与已登录后的 `1001` 也分开分类。

## 6. 可观测性

`RiskEventRecorder` 只写最小化 JSONL：时间、trackId、耗时、分类、ret/msg、在途数、会话 ID、请求序号和认证状态。它不写 Cookie、请求头、设备信息或播放 URL。

`xdl risk-report` 完全离线，支持不存在、空文件和坏行输入，并汇总会话内请求序号、延迟、间隔、并发与结果分布。

## 7. 配置与本地数据

`config.paths.xdl_home()` 是用户数据目录的单一来源，默认 `~/.xdl`，可由 `XDL_HOME` 覆盖。`Settings` 使用它生成 Profile、Cookie、任务库、设备信息和风控日志路径。

命令行当前可覆盖下载目录、音源后端、异步并发数，以及实验开关 `--experiment-rotate-device`。并发数必须大于 `0`；无效并发数或后端值会快速报错，不会静默修正或退回 Chrome。设备信息相关细项通过 Python `Settings` 配置。

## 8. 测试边界

测试分为：

- 领域纯函数测试。
- 用例与任务状态测试。
- SQLite 和文件续传测试。
- HTTP/Chrome/签名适配器的替身契约测试。
- CLI/TUI 行为测试。

默认测试不访问真实平台。测试通过证明本地契约和控制流，不证明当前账号、IP、时间段下的真实服务端可用性。

## 9. 已知结构债务

- `Facade.from_config()` 通过函数内导入调用装配根，而装配根又需要构造 `Facade`，形成被延迟导入掩盖的静态环。消除它需要调整公开工厂入口，留待有弃用计划的版本处理。
- Chrome 适配器仍包含已默认关闭的设备重置和高级诊断路径。它们有历史测试价值，但不属于默认 HTTP 主流程；公开入口经过弃用周期后应迁出或删除。
- 仓库尚未配置静态类型检查、代码风格检查和覆盖率插件；当前合并门禁主要依赖 pytest、`compileall` 与差异检查。
