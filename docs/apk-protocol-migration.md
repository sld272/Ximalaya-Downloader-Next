# APK 协议迁移方案

## 1. 目标与结论

将 `apk_protocol_demo` 原型中基于喜马拉雅 Android APK 9.5.1.3 的协议能力迁入 Ximalaya-Downloader-Next，作为新的可选 `apk` 音源后端，覆盖：

1. APK 短信协议登录与本地登录态持久化；
2. APK 专辑分页解析；
3. 当前账号获授权曲目的单曲解析与整专批量下载。

迁移采用“独立 APK 协议适配器接入现有应用内核”，不复制 demo 的 `BaseHTTPRequestHandler`、`DownloadManager`、`jobs.json` 或独立 Web 页面。Ximalaya-Downloader-Next 已有的用例、SQLite 任务库、`.part` 续传、失败重试、停止/恢复、CLI 和 WebUI 应继续作为唯一任务与交互体系。

迁移范围严格限定为新增 `--source-backend apk` 链路。原 `http`（WEB 协议）、`pc`（PC 协议）和 `chrome` 链路的 Source 实现、登录方式、请求端点、签名、Cookie、解析和下载行为保持不变；默认后端也不改变。公共层只允许增加带默认值、对旧调用透明的兼容性能力，并必须由原后端回归测试证明行为无变化。

### 1.1 强制隔离边界

```text
source_backend=http   → 原 HttpSource + PySignProvider（不改）
source_backend=pc     → 原 PcHttpSource（不改）
source_backend=chrome → 原 ChromeSource（不改）
source_backend=apk    → 新 ApkSource + ApkAuthProvider + ApkNativeBridge
```

实施时遵守以下硬约束：

- 不修改 `source_http.py`、`source_pc.py`、`source_chrome.py` 的协议行为；
- 不让 WEB/PC 后端调用 APK native sidecar，也不让 APK 后端读取浏览器 Cookie/Profile；
- 不把 APK 登录态写入 `{browser}-cookies.json`；
- 不改变 WEB/PC 的 URL 获取次数、重试规则、请求头、并发和默认音质选择；
- 必要的公共模型/端口扩展必须可选且有原值默认行为；
- APK 专属条件分支以能力检测或 `backend == "apk"` 收口，不侵入旧 Source；
- 任一 WEB/PC 既有测试出现行为差异即视为迁移失败。

## 2. 两个项目的能力现状

### 2.1 Ximalaya-Downloader-Next

目标项目采用端口与适配器架构：

```text
CLI / WebUI / Python API
          ↓
        Facade
          ↓
DownloadTrack / DownloadAlbum / Resume use cases
          ↓
 Source + MediaSink + TaskStore ports
          ↓
HTTP / PC / Chrome adapters + FileSink + SQLite
```

现有能力包括单曲/专辑/区间下载、音质协商、有界并发、错误分类、风控熔断、任务恢复、HTTP Range 字节续传和 WebUI 任务管理。平台易变逻辑已集中在 `adapters`，非常适合增加 APK 实现。

现有边界中需要为 APK 后端做的增量扩展：

- `Facade.login()` 假定登录是一次同步浏览器操作，无法表达“GeeTest → 发短信 → 验证码登录”的多阶段状态机；
- `Source.get_track(track_id)` 不接收目标音质，而 APK `mobile/download/v2/track` 一次请求需要 `trackQualityLevel`；该能力通过 APK 专属可选端口扩展，不要求改写现有 Source；
- `FileSink` 当前只掌握媒体 URL，无法由音源注入 APK CDN 所需的 `requestType: download` 请求头；
- 设置、CLI 和 WebUI 的 `source_backend` 枚举只有 `http|pc|chrome`。

### 2.2 apk_protocol_demo

demo 是单文件 Python Web 服务加 Java/Unidbg native sidecar：

```text
Python XmlyClient
  ├─ HTTP：passport.ximalaya.com / mobile.ximalaya.com
  ├─ DeviceState：device.json / xuid.json / auth.json
  └─ NativeSignerBridge（JSON Lines over stdin/stdout）
       └─ Java 17 + Unidbg
            ├─ liblogin_encrypt.so：手机号加密、登录参数签名
            ├─ libnativelib.so：XUID、x-tk
            ├─ libencrypt.so：下载 URL 解密/密钥
            └─ libc++_shared.so + assets/na.czl
```

离线测试覆盖登录路由、设备/认证持久化、专辑响应归一化、整专任务、断点续传、404 URL 刷新、重试、授权失败暂停和 APK CDN 请求头，共 15 项，当前全部通过。

## 3. 已还原调用链

### 3.1 APK 协议登录

```text
持久化 deviceId
  → native createXuid(deviceId) → 持久化 xuid
  → GeeTest 4 返回 fdsOtp
  → native encryptMobile(mobile)
  → GET passport/mobile/nonce/{ts}
  → TreeMap(key=value&) → native sign(...)
  → POST passport/mobile/sms/v3/send
  → 用户输入验证码
  → 新 nonce + native sign
  → POST passport/mobile/sms/v2/verify → smsKey/bizKey
  → 新 nonce + native sign
  → POST passport/mobile/login/quick/v2 → uid + token
  → 原子保存 auth
  → 请求 Cookie 增加 `1&_token={uid}&{token}`
```

约束：普通短信登录必须使用 `biz=1`；`fdsOtp` 必须包含 GeeTest 4 的五个字段，`lot_number` 不可复用；demo 还按手机号哈希实施 60 秒本地冷却。

### 3.2 专辑解析

普通展示清单：

```text
GET mobile/v1/album/track/v3/ts-{ts}
  ?albumId=&pageId=&pageSize=&device=android&isAsc=true
  → normalize tracks/list/trackList 等响应形态
GET mobile/v1/album?albumId=...
  → 补专辑标题等头信息
```

用于批量下载的授权清单：

```text
xuid + uid → native ticket("b=downloadTrack&s=batch_download&u={uid}")
  → x-tk header
  → GET mobile/download/v1/album/{albumId}/{page}/true/ts-{ts}
  → tracks.list + pageId/maxPageId/totalCount
  → 仅处理 isAuthorized=true 的曲目
```

### 3.3 单曲解析与批量下载

```text
对专辑中的每一个 trackId（逐集执行，禁止共用或缓存连接）
  → native ticket("b=downloadTrack&s=download&u={uid}") → 本集 x-tk
  → GET mobile/download/v2/track/{trackId}/ts-{当前时间戳}
       ?trackQualityLevel=&device=android
  → 检查本集 isAuthorized
  → 优先使用本集明文 downloadAacUrl/downloadUrl/playPath...
  → 否则 native decryptDownload(本集 value, downloadEncryptVersion)
  → HTTP GET 本集媒体 URL，header: requestType=download
  → 下载完成后丢弃 URL
```

APK 下载连接采用强制短生命周期规则：

1. 整专授权清单只提供 trackId、顺序、标题和授权状态；即使响应中带 URL，也不作为正式下载连接；
2. 每一集开始下载前必须单独请求一次 `mobile/download/v2/track/{trackId}`；
3. 下载 URL 不写入 SQLite、日志、任务结果或任何跨集/跨进程缓存；
4. `.part` 任务恢复时，先按 trackId 和 quality 重新获取本集连接，再从当前字节数发送 `Range`；
5. 当前连接遇到 `401/403/404` 时立即作废，重新获取本集连接后最多再试一次；
6. `429/5xx` 和网络瞬态错误沿用受限退避，但不得把一个 trackId 的连接用于另一集；
7. 一集完成或最终失败后立即丢弃连接，下一集重新走完整解析链路。

demo 自带的下载调度规则（全局单 worker、`jobs.json`、连续三集失败暂停）不迁移；等价能力由现有 `DownloadAlbumUseCase`、`SqliteTaskStore` 和文件下载能力承担。应迁移的是 APK 专属请求语义：授权清单、逐集 `x-tk`、逐集 URL 获取/解密、CDN header 和失效后单次重新解析。

## 4. 目标模块设计

建议新增：

```text
src/xdl/
├─ ports/
│  └─ ports.py                    # 仅增加可选 Auth/QualityAware 能力
├─ adapters/apk/
│  ├─ __init__.py
│  ├─ source.py                   # ApkSource，实现 Source
│  ├─ auth.py                     # ApkAuthProvider，多阶段短信登录
│  ├─ client.py                   # APK HTTP 请求与响应分类
│  ├─ native_bridge.py            # Java sidecar 生命周期/JSONL RPC
│  ├─ state.py                    # device/xuid/auth 原子持久化
│  ├─ models.py                   # 内部 DTO 与 quality 映射
│  └─ assets.py                   # 资产发现、hash/版本校验
├─ adapters/
│  └─ apk_sink.py                 # APK 专属 CDN header/连接刷新适配
├─ config/
│  └─ apk.py                      # 端点、包版本、UA、captcha ID
└─ native_signer/                 # 建议独立源码目录或独立构建子项目
```

运行数据放到 `XDL_HOME/apk/`，与浏览器身份明确隔离：

```text
~/.xdl/apk/device.json
~/.xdl/apk/xuid.json
~/.xdl/apk/auth.json        # 0600，日志与 API 永不返回 token
~/.xdl/apk/native/          # 若采用外置资产安装模式
```

不要把 APK token 写入 `{browser}-cookies.json`，也不要把浏览器 Cookie 注入 APK 身份。两套协议的设备档案和认证态应完全独立。

## 5. 关键接口调整

### 5.1 增加认证端口

建议新增显式多阶段端口，而不是继续扩大 `Source.interactive_login()`：

```python
class AuthProvider(Protocol):
    def status(self) -> AuthStatus: ...
    def login_config(self) -> LoginConfig: ...
    def send_sms(self, mobile: str, fds_otp: dict) -> SmsChallenge: ...
    def verify_sms(self, code: str) -> AuthStatus: ...
    def logout(self) -> None: ...
```

`Facade` 增加 `auth_status()`、`send_login_sms()`、`verify_login_sms()`、`logout()`。保留现有 `login()` 供浏览器后端兼容；当后端为 `apk` 时，CLI 可运行交互向导，WebUI 使用分步 API。

GeeTest 必须在 WebUI 前端完成，后端只接收并严格校验 `fdsOtp`。API 返回值需要脱敏，绝不回显 mobile 密文、token、Cookie 或完整设备标识。

### 5.2 APK 专属音质感知能力

APK 下载接口一次只接收一个 `trackQualityLevel`。不应为了适配现有 `get_track(track_id)` 而连续请求 0–3 四个档位，这会放大请求量；也不应为此修改 HTTP/PC Source 的方法签名。增加一个仅由 `ApkSource` 实现的可选能力：

```python
@runtime_checkable
class QualityAwareSource(Protocol):
    async def get_track_for_quality(
        self, track_id: str, quality: Quality
    ) -> Track: ...
```

应用层增加一个兼容性解析 helper：如果 Source 实现 `QualityAwareSource`，调用 `get_track_for_quality()`；否则继续原样调用 `get_track()`。`HttpSource`、`PcHttpSource`、`ChromeSource` 不需要修改，实现与调用次数保持原样。

APK 后端维护显式映射表，并通过契约测试固定 `high|standard|low → trackQualityLevel`。映射在真实响应校准前不要凭名称猜定；先记录每档返回的 codec、bitrate 和 fileSize，再固化映射。

`list_formats()` 对 APK 后端应标为“探测型操作”：可以串行请求各 APK 档位，但必须去重、限速，默认下载不走该路径。

### 5.3 APK 专属媒体下载适配器

为了不改变 WEB/PC 下载请求头和 `FileSink` 行为，优先为 APK 装配独立 `ApkMediaSink`。它复用文件命名、`.part` 和 Range 语义，但只在 APK 链路增加 `requestType: download`，并接受用于重新解析本集连接的回调上下文。

```python
if backend == "apk":
    source = ApkSource(...)
    sink = ApkMediaSink(source=source, ...)
else:
    sink = FileSink(...)  # 原 WEB/PC/Chrome 路径不变
```

为了让 APK sink 获得重新解析所需的 trackId/quality，同时不改变旧 `MediaSink`，新增可选能力：

```python
@runtime_checkable
class TrackResolvingMediaSink(Protocol):
    def write_track(
        self,
        *,
        track_id: str,
        quality: Quality,
        target_path: str,
        reporter: ProgressReporter,
        cancel=None,
        progress_sink=None,
        expected_total: int = 0,
    ) -> None: ...
```

下载用例只增加一次能力分派：sink 实现 `TrackResolvingMediaSink` 时调用 `write_track()`，由 `ApkMediaSink` 在方法内部按 trackId/quality 获取新 URL；否则继续执行当前的“Source 先返回 `PlayUrl` → `FileSink.write(url, ...)`”流程。这样旧后端的方法调用、URL 解析和下载请求均不改变。

如果为了去除重复代码必须扩展公共 `MediaSink.write()`，新增参数必须有默认值，并确保旧调用产生与当前完全相同的请求；`http|pc|chrome` 仍装配原 `FileSink`。APK 适配器仅设置 `requestType: download`，不得把认证 Cookie、`x-tk` 或设备档案传给 CDN/写入任务库。

连接刷新只属于 `ApkMediaSink`/APK 下载协调器：它持有 trackId 和 quality，不持久化 URL；首次请求以及 `.part` 恢复都调用 `ApkSource.get_track_for_quality()`。`401/403/404` 时清空当前 URL，重新解析一次；刷新预算耗尽后按现有错误体系保存为失败/待恢复。原 `FileSink` 不新增 WEB/PC 的 URL 刷新行为。

### 5.4 允许修改与禁止修改的文件边界

| 类型 | 文件/目录 | 规则 |
|---|---|---|
| 新增 | `src/xdl/adapters/apk/**`、`src/xdl/adapters/apk_sink.py` | APK 协议全部实现收口于此 |
| 新增 | `src/xdl/config/apk.py`、`native_signer/**`、APK tests/fixtures | 不被旧后端导入或启动 |
| 增量 | `composition.py`、`settings.py` | 只注册 `apk` 与 APK 配置；旧分支保持原装配 |
| 增量 | `ports.py`、下载用例 | 只增加可选 capability 检测和兼容分派 |
| 增量 | CLI/Web schema/UI | 只增加 `apk` 选项和 APK 登录界面 |
| 禁止行为变更 | `source_http.py`、`source_pc.py`、`source_chrome.py` | 不改协议、登录、header、Cookie、解析、重试 |
| 禁止行为变更 | `sink_file.py` | 继续作为 WEB/PC/Chrome sink；不增加 APK header/刷新逻辑 |

### 5.5 Source 生命周期与并发

`ApkSource.open()` 启动并 `ping` 一个长驻 Java sidecar；`close()` 负责关闭 stdin、等待退出并在超时后终止。所有 RPC 通过一个锁串行化并带：

- 启动超时、单次调用超时、EOF/非 JSON/错误响应分类；
- 子进程崩溃后最多重启一次；
- stderr 独立收集且脱敏，不能让管道塞满；
- 进程关闭幂等；WebUI 重载设置时必须释放旧 sidecar；
- native 解密和 `x-tk` 调用优先复用长驻进程，避免 demo 中部分操作每曲启动一次 JVM 的高开销。

`max_concurrency` 可以继续控制网络任务，但 native RPC 先保持串行。APK 后端第一版建议默认并发 1，经小样本观测后再开放更高值。

## 6. 配置与资产分发

新增 `Settings` 字段建议：

```text
source_backend = "apk"
apk_state_dir
apk_java_path
apk_signer_jar
apk_libcxx_path
apk_login_so_path
apk_xuid_so_path
apk_encrypt_so_path
apk_asset_dir
apk_version = "9.5.1.3"
apk_request_timeout
apk_native_timeout
```

配置路径默认从 `~/.xdl/apk/native` 或安装包资源目录派生；CLI/Web 设置只显示路径和版本，不显示认证内容。

native bundle 当前约 33 MiB（JAR 31 MiB，四个 `.so` 约 2.1 MiB，另有 `na.czl`），要求 Java 17+。迁移前必须完成以下发布门禁：

1. 确认四个 APK `.so`、`na.czl` 及其衍生分发的许可/来源；
2. 为每个资产建立 `manifest.json`：APK 版本、ABI、大小、SHA-256；
3. 启动时逐项验 hash，版本不匹配快速失败；
4. 生成 signer JAR 必须可复现，Maven 依赖锁定并产出第三方许可证/SBOM；
5. `.gitignore` 当前全局忽略 `*.so`，若确认允许入库需为受控资产目录增加精确反向规则，禁止泛化；
6. 若不能随主仓库分发，提供显式的 `xdl apk-assets install <bundle>` 本地安装流程，不能运行时静默联网下载；
7. Windows/macOS/Linux 都由 Unidbg 仿真 ARM64 `.so`，但仍需分别验证 Java 路径、子进程信号与文件权限。

不要直接提交 demo 的预编译 `target/native-signer.jar` 而不提交可构建源码、锁定版本和校验清单。

## 7. 逐阶段实施计划

### Phase 0：基线与资产门禁

- 固定 demo 15 项离线测试为迁移前契约；
- 为 demo 中关键响应建立脱敏 fixture：登录成功/失败、三种专辑 shape、授权/未授权曲目、明文/加密 URL、404/429/5xx；
- 记录 native 资产 SHA-256、版本和来源，完成分发决策；
- 修复或隔离目标仓库已有的计时浮点断言不稳定，保证迁移前主分支基线稳定。
- 为 `http`、`pc`、`chrome` 建立回归快照：Source 调用次数、请求端点、请求头、Cookie、所用 sink 与恢复行为；后续每阶段强制比对。

验收：不访问真实接口即可重复验证所有解析和控制流；资产校验失败时给出 `ConfigError`。

### Phase 1：native bridge 与状态层

- 迁入 `NativeSigner.java` 源码及 Maven 构建；
- 实现 `ApkNativeBridge`，覆盖 ping/encryptMobile/sign/createXuid/ticket/decryptDownload；
- 实现 `ApkStateStore`，原子写入、`0600` 权限、损坏文件恢复、token 脱敏；
- 增加 sidecar 生命周期、并发、崩溃重启和超时测试。

验收：固定输入的签名、XUID、ticket、解密结果与 demo 一致；连续调用不反复启动 JVM；关闭后无残留进程。

### Phase 2：APK HTTP Source

- 实现统一 `ApkClient` 请求层和 ret/msg → `AuthError`/`NetworkError`/`ApiError`/`RiskControlError` 映射；
- 实现普通专辑分页和授权下载清单解析；
- 实现逐集单曲下载信息、逐集 `x-tk`、URL 解密；
- 实现 `QualityAwareSource` 可选能力，旧 Source 不修改；
- 在 `composition.py` 注册 `apk`，CLI/Settings/Web schema 增加枚举；
- 先只接入现有测试替身，不启用登录 UI。

验收：`Facade.download_track/album/resume` 用 APK fixture 跑通；N 集必须观测到 N 次独立 track 下载信息请求和 N 个对应 `x-tk`，SQLite、区间筛选、停止和续传仍由现有用例完成；HTTP/PC/Chrome 回归快照无变化。

### Phase 3：多阶段 APK 登录

- 新增 `AuthProvider` 和 Facade 方法；
- Web API 增加 config/status/sms/verify/logout，沿用 WebRuntime 单操作槽；
- WebUI 增加 APK 登录弹窗和 GeeTest 4 集成，仅在选择 `apk` 后端时显示；
- CLI 提供 `xdl --source-backend apk login` 向导：打开本地 WebUI 完成人机验证，或接受用户显式提供的 `fdsOtp` 文件；
- 保留每手机号 60 秒冷却、一次性 lot、`biz=1` 和错误原样分类。

验收：刷新进程后仍能读取 uid/token 登录态；logout 只清 APK auth，不删除 device/xuid；日志、Web API 与错误详情中无 token/手机号。

### Phase 4：下载语义补齐

- 为 `Quality` 校准 APK `trackQualityLevel`；
- 装配 APK 专属 `ApkMediaSink`，原 `FileSink` 继续服务 WEB/PC/Chrome；
- 强制每集首次下载前重新解析 URL，禁止 URL 持久化/跨集复用；
- 强制 `.part` 恢复前重新解析本集 URL；
- 实现 `401/403/404` 作废 URL 后最多一次重新解析；
- 将 demo 的连续授权失败保护映射到现有批次熔断/错误分类，不另造任务状态机；
- 风控日志增加 `backend=apk`、endpoint kind、quality level，仍禁止记录 URL/header/token。

验收：明文与 native 加密 URL 均可下载；每集均先获取独立新连接；Range 续传前也重新取连接并携带 `requestType: download`；失效连接最多刷新一次，429/5xx 受限退避；WEB/PC 下载请求不新增 `requestType`，URL 获取与重试行为无变化。

### Phase 5：真实小样本与发布

- 使用授权账号按 1 曲、3 曲、小专辑顺序低频验收；
- 覆盖免费、已购、未授权、不同音质、URL 过期、进程重启恢复；
- macOS、Windows、Linux 各跑一轮 Java sidecar 冒烟；
- 更新 README、architecture、WebUI 文档和依赖/资产说明；
- 保持 `apk` 非默认，至少经过一个版本观测后再评估默认后端。

## 8. 测试矩阵

| 层级 | 必测项 |
|---|---|
| state | deviceId 稳定、xuid 与 deviceId 绑定、auth 原子写/权限/清除、坏 JSON |
| native RPC | 每个 op、并发串行、超时、坏行、EOF、崩溃重启、幂等 close、hash 不匹配 |
| HTTP | GET/JSON POST、Cookie 格式、x-tk scene、超时、非 JSON、ret 分类、敏感字段脱敏 |
| auth | GeeTest 字段、captcha ID、lot 防复用、冷却、biz=1、nonce 缺失、verify/login 两步失败 |
| album | 多页、三种 shape、空专辑、标题补全、授权过滤、序号与总数 |
| track | quality 映射、isAuthorized、明文 URL、v0/v2 解密、无 URL、临时 URL 刷新 |
| apk per-track | N 集产生 N 次独立下载信息请求；每次使用本集 x-tk；URL 不跨集、不落库 |
| apk resume | 已有 `.part` 时先重新取 URL，再按新 URL 发 Range；不得复用进程重启前 URL |
| apk refresh | 401/403/404 作废连接并仅刷新一次；新连接仍绑定同一 trackId/quality |
| apk sink | 仅 APK CDN 请求带 requestType=download；服务端不支持 Range、空文件、取消、恢复 |
| legacy regression | HTTP/PC/Chrome 的 Source、登录、端点、header、Cookie、URL 获取次数、FileSink 行为不变 |
| frontend | backend 枚举、APK/浏览器登录 UI 切换、API 严格校验、设置重载释放 sidecar |
| packaging | wheel/sdist 是否含预期源码/资产、Java 17 缺失提示、三平台路径与权限 |

离线 CI 禁止真实网络。线上协议验收单独以手工/受控 job 执行，不保存凭据和原始响应。

## 9. 数据迁移与兼容性

- 现有 `tasks.db` schema 无需为 APK 后端新增字段，任务仍以 trackId/albumId/quality/path 为核心；
- 已有浏览器 Cookie 和 Profile 不迁移、不删除；用户切回 `http|pc|chrome` 可继续使用；
- APK auth 是新增身份域，不自动从浏览器 Cookie 推导；
- APK 媒体 URL 永不进入任务库；恢复依据仍是 trackId、quality、目标路径和 bytes_done；
- 若未来需要任务精确绑定后端，应给任务表新增可空 `source_backend`，旧记录默认按当前后端恢复。第一版可先在 UI 明确提示“请用创建任务时的后端恢复”，但发布前更推荐完成该 schema 迁移；
- 设置文件读取未知/缺失 APK 字段时使用安全默认值，旧配置保持可用；
- `Facade.login()` 和现有 Source 方法保留兼容期，新参数必须有默认值，避免破坏第三方 Python 调用方。

## 10. 风险与回滚

| 风险 | 处理 |
|---|---|
| APK 版本升级导致 JNI 方法/算法变化 | bundle 绑定 APK 版本和 hash；新旧 bundle 可并存；快速失败而非猜测兼容 |
| native 资产不可合法分发 | 主仓库仅保留桥接源码和本地安装器，资产由用户从本地授权 APK 导入 |
| Java/Unidbg 增大安装复杂度 | `apk` 保持 optional；启动前诊断 Java 17、资产与 sidecar ping |
| 多阶段登录破坏现有浏览器登录 | 新 AuthProvider；两类身份和 UI 分支隔离，不复用缓存文件 |
| quality 契约不匹配 | 先采样校准映射；端口显式传入 Quality，避免四倍请求探测 |
| 临时 URL 过期 | 用例级最多刷新一次；持续失败进入可恢复任务状态 |
| 公共层扩展误伤旧后端 | 可选能力 + 默认参数；旧 Source 不实现新端口；HTTP/PC/Chrome 全量回归作为合并门禁 |
| 高并发触发风控 | APK 后端默认 1；沿用现有熔断和可选 risk-poll，不自动提高并发 |
| 敏感数据泄漏 | auth 0600、统一 redact、测试日志扫描、风险日志不含 URL/header/token |

回滚只需切换 `source_backend` 回 `http` 或 `pc`。由于旧 Source 和旧 `FileSink` 行为不变，回滚不依赖协议代码恢复；APK 适配器不改变下载文件格式。数据库 schema 变更必须使用向前兼容的新增列，并在回滚版本读取时可忽略。

## 11. 建议的首批提交切分

1. `test: freeze apk protocol fixtures and native bridge contract`
2. `feat: add versioned apk native bridge and state store`
3. `feat: add apk source backend for album and track resolution`
4. `feat: add apk-only quality-aware resolution and media sink`
5. `feat: add multi-step apk sms authentication APIs and WebUI`
6. `docs: document apk assets, setup, diagnostics, and migration`

每个提交都应独立通过离线测试；native 资产与 Python 业务改动分提交，便于许可审查和回滚。每个提交必须同时运行原 HTTP/PC/Chrome 测试，禁止以修改旧后端期望值的方式让回归通过。

## 12. 当前基线验证

- `apk_protocol_demo`: `python3 -m unittest -v test_server.py` → 15/15 通过；
- Ximalaya-Downloader-Next: 使用仓库现有虚拟环境运行 `pytest -q` → 322 通过、1 个既有不稳定断言失败；失败位于 `test_album_risk_poll_recovers_and_continues`，`initial_wait=0` 时 `risk_wait_seconds` 得到约几十微秒而测试严格要求 `0.0`，与 APK 迁移无关；
- 新分支与工作树均从 `c3e70c2` 创建，迁移方案只在新工作树中落地。
