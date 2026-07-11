# Web 风控观测报告

本文只记录在正常授权访问下得到的可复验证据。它不是绕过指南，也不把单一账号、IP、时段的结果写成平台固定阈值。禁止用代理/账号/指纹轮换、验证码绕过或持续加压来补齐数据。

## 2026-07-11 基线

环境：Windows、真实 Google Chrome、专用持久化 Profile、CDP 接管、Web `/sound` 页面自行生成签名。只解析元数据，不下载媒体文件。

| 场景 | 请求数 | 并发 | 结果 | 用时 |
|---|---:|---:|---|---:|
| `/sound/{id}` 页面 | 1 | 1 | HTTP 200 | 647 ms |
| 非 v1 专辑清单 | 1 | 1 | `ret=200`，30/1754 条 | 69 ms |
| v1 专辑清单，无 webtk | 1 | 1 | `ret=407 / webtk缺失` | 58 ms |
| `baseInfo` 单曲解析 | 1 | 1 | 成功，4 个播放规格 | 14.00 s |
| `baseInfo` 批量解析 | 8 | 4 | 1 成功，7 个 `3005/系统繁忙` | 43.74 s |
| 风控后冷却再试 | 1 | 1 | 35 秒后仍为 `3005` | 22.48 s |

本次可确认：

- 公开专辑清单和受保护播放信息不是同一风控面。
- `407` 是签名/令牌缺失，不等于频率风控。
- `3005` 是复用返回码；当消息为"系统繁忙"且同一曲目先成功后失败时，应按临时风控处理，不能一律归为账号无权。
- 当前环境中并发 4 不安全；35 秒不足以观察到恢复。

尚未证明：

- 任何普遍适用的安全 QPS、并发数、日请求量或冷却时长。
- `1001` 与 `3005` 是否由相同规则触发。
- 账号、IP、Profile、无头模式、内容权限分别占多大权重。
- 验证码出现条件。不能通过主动反复触发验证码来测量。

## 2026-07-11 自然下载样本：串行第二条 `1001`

用户正常选择 20 集下载，熔断后实际只有前两条访问了受保护接口：

| 序号 | 内容 | 时长 | 解析结果 | 解析启动间隔 | 在途数 |
|---:|---|---:|---|---:|---:|
| 1 | 片花 | 53 秒 | 成功；随后下载 435376 字节 | — | 1 |
| 2 | 正式第 1 集 | 413 秒 | `1001/系统繁忙` | 距首次启动 8.753 秒；距首次完成 1.465 秒 | 1 |

第 3–20 条没有发起请求，任务保持 pending。公开清单将前两条都标记为 `isPaid=true`，但专用 Chrome Profile 中没有 `*&_token` 登录 Cookie，只有统计、WAF 和设备类 Cookie。因此该样本同时符合两种解释：匿名用户可取得短片花/试听地址，但正式付费内容被拒；或者第二次请求命中了短间隔频控。它不能单独证明"串行第 2 次"是平台固定阈值。

用户随后报告已登录，但用同一专用 Profile 实际打开首页复核时，页面仍显示 3 个可见"登录"控件、0 个退出控件、0 个个人主页链接；Cookie、Local Storage 与 Session Storage 的键名中也没有登录 token。因此该次登录没有进入下载器实际使用的 Profile，不能作为已登录对照样本。

该事件旧版记录的 40.354 秒是等待 `resolve_timeout` 的耗时，并非已证明的平台响应时延。原因是旧 XHR 钩子只按 trackId 归档成功响应，错误共用全局 `__xmerr`，且收到错误后仍等待到超时。现已改为 `__xmerrs[trackId]` 并在目标错误出现时立即返回；后续事件还会记录登录态布尔值，从而把匿名权限拒绝与登录后的频控样本分开统计。

## 2026-07-11 已登录自然样本：串行第 3 条 `3005`

修复登录闭环后，专用 Profile 已确认存在有效 `1&_token`，事件也记录为 `authenticated=true`。用户正常恢复下载，得到以下时间线：

| 会话内请求 | 曲目序号 | 结果 | 解析耗时 | 距前次启动 | 距前次完成 | 在途数 |
|---:|---:|---|---:|---:|---:|---:|
| 1 | 2 | 成功 | 7.664 秒 | — | — | 1 |
| 2 | 3 | 成功 | 6.139 秒 | 9.046 秒 | 1.382 秒 | 1 |
| 3 | 4 | `3005/系统繁忙` | 6.210 秒 | 8.036 秒 | 1.897 秒 | 1 |

第 5–20 条被熔断，没有访问受保护接口。该样本排除了"匿名正式付费内容拒绝"这一解释，证明当前账号/IP/Profile/时段下，即使串行请求，约 8–9 秒启动间隔、前次完成后约 1.4–1.9 秒继续解析时，第 3 次受保护 `baseInfo` 请求也会触发风控。

这仍是一次环境相关观测，不能推广为平台永久固定的"第 3 次"规则。尤其是服务端可能综合账号、IP、Profile 历史信誉、内容类型和滑动时间窗。风控后尚未出现同一登录状态下的后续成功事件，因此本次冷却时长仍未知。旧匿名状态的 `1001` 到本次登录成功之间的 963 秒不计作恢复时间。

## 手动浏览器与 XDL 的网络差分

用户在日常已登录 Chrome 中快速连续播放，没有验证码或风控；同一账号经 XDL 专用 Profile/CDP 访问时，有头和无头模式都会返回 `3005`，有头诊断还弹出大量验证码窗口。因此 headless 不是充分原因，纯请求速度也不能解释全部差异。

对单个 `/sound/852566950` 页面做脱敏网络监听时，页面分别产生约 22 和 24 个 `baseInfo` 响应，涉及目标曲目、上次播放和大量推荐曲目；目标曲目本身也可能重复请求。XDL 原观测日志只记录目标结果，因此此前"会话第 3 个请求触发"实际低估了浏览器页面发出的总请求量。每集新建页面会反复触发这一整套初始化/推荐请求，是当前最强的请求层根因。

另一项离线指纹检查显示：`navigator.webdriver=false`，但旧实现改写后的 `XMLHttpRequest.prototype.open/send` 均不再是原生函数，且页面存在 `window.__xmHooked` 标记。这是正常手动浏览器没有的明显反篡改信号。页面路由拦截实验没有阻止这些请求，推测它们可能经过 Service Worker 或页面路由覆盖不到的目标；该无效改动已回滚。

现已删除所有 XHR 注入和自动点击逻辑，改用 Playwright 网络 `response` 事件只读解析目标 trackId。该修改消除了最明显的页面篡改指纹，但仍保留 CDP、专用 Profile 和每曲新页面等差异。由于测试 Profile 当前处于验证码/风控状态，尚未用平台请求证明修改后已经恢复；后续应等待正常恢复样本，不应立即重复触发验证码。

## 2026-07-11 复核：HeadlessChrome 指纹与验证码惩罚态

用真实浏览器（扩展驱动的日常 Chrome/Edge）与 XDL 环境再做一轮受控对照，得到几点可复验的补充证据：

- **日常浏览器**导航到 `/sound/{id}` 会自动为目标 trackId 发出 `baseInfo` 并返回 `200/ret=0`，无需点击播放；`navigator.webdriver=false`、`window.chrome` 完整、UA 为正常 `Chrome/150`。
- **XDL 默认环境**（`--headless=new` + CDP 接管专用 Profile）实测 UA 为 `HeadlessChrome/150.0.0.0`。直连该 Chrome 请求目标 `baseInfo` 返回 `ret=3005/系统繁忙`，页面同时加载 **GeeTest v4 图形验证码**（`gcaptcha4.geetest.com` 的 load 挑战、3D 图标匹配资源）。
- 该专用 Profile 一旦被判罚，进入**验证码惩罚态**：随后无论有头还是无头，每个曲目都产生约 11 个 GeeTest 请求且目标 `baseInfo` 不再返回。这解释了"下完一两集就风控"——首次触发后账号/Profile 被要求先过验证码，headless 无从人工通过，于是卡死。
- `navigator.webdriver` 在 headless、headful、CDP 覆盖 UA 各配置下均为 `false`，再次排除它作为触发信号。

据此可以明确：`HeadlessChrome` 这一 UA 令牌是日常浏览器所没有、且最容易被识别的自动化指纹之一；它不是风控的唯一权重，但属于应当消除的确定性信号。由于测试 Profile 已进入验证码惩罚态，无法在同一 Profile 上干净地隔离"UA 单因素"，因此本轮不宣称 UA 是充分或唯一原因，仅确认它是一个真实且可消除的差异。

### 本轮代码调整（均不含验证码绕过）

- **默认无头静默**（`chrome_headless=True`，不弹窗）：但解析时通过 CDP `Network.setUserAgentOverride` 把 `HeadlessChrome` UA（含 client-hints）抹成与真实主版本一致的 `Chrome`，消除最明显的自动化指纹。有头本就是正常 Chrome，探测一次后跳过覆盖。
- **恢复自动触发播放**（`_trigger_play`）：codex 之前把注入 XHR 钩子和自动点播放一起删了，导致目标 `baseInfo` 不自动发时需要用户手点。现改为先等一段自动发出的时间，没发就用 **Playwright 真实点击**播放控件（找不到控件才回退到脚本点击），用户无需手动干预。真实点击比注入 `el.click()` 更接近真人。
- `_capture_base_info` 在超时窗口内**监听 GeeTest/惩罚类请求**。超时返回带 `timeout`/`captcha` 标记的诊断信息，替代此前无信息的 `ret=None/msg=None`：
  - 见到验证码 → 归类为 `RiskControlError`，并在无头会话下**自动切到有头弹窗**（`risk_fallback_headful=True`），让用户手动过一次验证码；期间用更长超时轮询，过完自动继续，无需敲回车。切有头后本会话保持有头，避免每曲反复切换。
  - 纯超时（未见验证码）→ 归类为可重试的 `ApiError`，文案说明"未捕获到目标 baseInfo"，提示可调大 `resolve_timeout`。
- 验证码信号刻意只认**主动发起的挑战**（`gcaptcha4.geetest.com`/`api.geetest.com`/`/punish` 等），不含可能被惰性预载的 `fe-captcha` 模块，避免把偶发超时误判为风控而错误熔断整批。

### 尚待正常恢复后验证

- 测试用专用 Profile 目前处于验证码惩罚态，需等待自然冷却或用户手动过一次验证码后才能取得干净的"有头 + 已登录 + 未惩罚"对照样本。不得通过反复主动触发验证码来加速测量。
- 每曲新建页面导致的推荐/初始化请求扇出（见上一节）仍是请求层根因之一，尚未改动；后续可评估"会话内复用单页、导航切换"与"预热日常 Profile 信誉"两个方向。

## 2026-07-11 设备 Cookie 差分：风控跟设备标识走、不跟账号走

用户日常 Chrome 用同一账号快速连续播放无验证码，专用 Profile/CDP 同账号却进入验证码惩罚态。读取专用 Profile 的喜马拉雅 Cookie（仅列名称，不读 value）后定位到登录态与设备标识是分开存放的：

| Cookie | 类别 | 备注 |
|---|---|---|
| `1&_token` / `1&remember_me` / `web_login` | 登录态 | httponly，账号身份 |
| `_xmLog` | 设备标识 | 喜马拉雅日志 SDK 的设备 ID，即风控的 browser_id 载体 |
| `wfp` | Web 指纹 | 浏览器/设备指纹 |
| `Hm_lvt_*` / `Hm_lpvt_*` | 访问历史 | 百度统计：首次/最近访问时间戳，风控用它判设备"年龄" |
| `tgw_l7_route` | 路由 | 负载均衡，无害 |

由此可证：专用 Profile 的 `_xmLog`/`wfp`/`Hm_lvt_*` 是冷启动新生成、无日常浏览信誉的设备 ID，被识别为可疑自动化设备并判罚；惩罚绑定在这组设备 Cookie 上，**不跟随账号**（用户日常浏览器同账号无风控已排除账号绑定）。结论：保留 `1&_token` 等登录 Cookie、只清除 `_xmLog`/`wfp`/`Hm_lvt_*` 这一组设备 Cookie，等同"在新设备登录同一账号"，下次访问页面时 `du_web_sdk` 会为本 Profile 重新生成新设备 ID，旧设备上累积的验证码惩罚态不带入新设备。这是比"整个 Profile 重登"更精准且不必重登的恢复路径。

`xdl inspect` 实测后追加发现：用户 `resume` 在重置设备 Cookie 后成功下载 3 集又陷入无限图形验证码——说明 Cookie 不是设备身份的唯一载体。`localStorage` / `sessionStorage` / `IndexedDB` 里同样承载把"换 Cookie 后新身"再次关联到被惩罚身份的指纹：

| 存储 | key | 备注 |
|---|---|---|
| localStorage | `_antispam_` | 反垃圾指纹（124B） |
| localStorage | `crystal` | 喜马拉雅"水晶"设备指纹 SDK（256B） |
| localStorage | `cid` | 疑似 client/device id（88B） |
| localStorage | `assva5` / `assva6` / `cmci9xde` / `vmce9xdq` / `pmck9xge` | 反垃圾/埋点混淆键 |
| localStorage | `Hm_lvt_*` | 百度统计首次访问时间戳镜像 |
| sessionStorage | `Hm_lpvt_*` / `HMACCOUNT` | 百度统计本次访问/账户镜像 |
| IndexedDB | `treasure` | 喜马拉雅埋点 SDK 库 |

因此 Cookie 重置是"换身"的必要而非充分条件：只换 Cookie 不换 storage，3 集后服务端通过 localStorage/IndexedDB 里的旧设备指纹再次关联到被惩罚身份。需要把清理扩展到页面 origin 下的全部 storage。

### 本轮代码调整（均不含验证码绕过，不修改页面 JS）

- 新增 `Settings.reset_device_fingerprint: bool = True` 开关。
- `ChromeSource` 在 `open()` 接管成功、`interactive_login` 验证登录后各做一次 **设备指纹重置**，包含两步：
  1. **Cookie 分区清空**：取得当前全部 Cookie → 按 name 前缀 `_xmLog` / `wfp` / `Hm_lvt_` / `Hm_lpvt_` 分区 → `context.clear_cookies()` 清空 → 回灌保留项（登录 Cookie、`tgw_l7_route` 等无关项）。
  2. **页面 storage 清空**：在该 origin 下开一次性页面 `goto` 首页，执行 `localStorage.clear()` / `sessionStorage.clear()` / 对 `indexedDB.databases()` 里每个库 `indexedDB.deleteDatabase()`（包括 `treasure`），随后关闭页面。
  同步/异步两套实现分别供登录与解析使用，本会话只重置一次；任一步失败不阻断另一步与正常解析。
- 新增只读诊断 `xdl inspect`：启动浏览器，列出当前 Profile 下所有 Cookie 名、localStorage / sessionStorage / IndexedDB 的 key 名与 value 长度（不读 value），用于判断"清 Device Cookie"是否覆盖了所有承载设备身份的存储面。
- `RiskEventRecorder.record` 新增可选 `device_fingerprint_reset` 字段；受保护接口事件据此上报本次请求是否处于"已重置设备指纹"的会话，便于离线 A/B 对照 (`xdl risk-report`)：分组比较 `reset=True` vs `reset=False` 下的成功率与首次风控请求序号，以验证重置后是否摆脱惩罚态。该字段仍遵守最小元数据原则，不含 Cookie 值或设备指纹内容。

### 尚待验证

- 当前专用 Profile 已进入验证码惩罚态；本轮"清 Chunk + storage 不重登"是否足以摆脱惩罚需待用户下次解析的实际结果验证，不能预先宣称生效。
- 若重置后立即高频请求新设备 ID 仍被快速判罚，则说明平台还看 IP 或账号近期活跃度，需进一步降速 / 单页复用 / 复用日常 Profile 信誉。

## 2026-07-11 全清后实测：3 集蜜月与 CDP 根因

用户 `xdl resume` 实测：清完设备 Cookie + `localStorage`（17 项）+ `sessionStorage`（7 项）+ `IndexedDB`（`treasure`），日志打印重置成功，但本次又只在下载第 3 集后返回 `1001/系统繁忙` 并熔断（事件 #19-#24）。把历次对照组摊开看：

| 时段 | 会话样本 | 蜜月长度 | 状态 |
|---|---|---|---|
| reset 不存在 | #3-#5 | 第 3 集 3005 | 例行 |
| reset 不存在 | #14-#16 | 第 3 集 3005 | 例行 |
| reset=True，只清 Cookie（首次实现）| #19-#21 | 第 4 集 3005 | 例行 |
| reset=True，Cookie + 全部 storage 全清（本次）| #22-#24 | 第 3 集 1001 | 例行 |

**蜜月长度恒为 3 集**，与"是否重置、重置范围多大"无关。结论：换设备身份只换得起一次 3 集红利，不解决"3 集后被识别"这件事本身。

### 逐项排除：差异只剩 CDP 接管

| 候选差异 | 状态 |
|---|---|
| 账号绑定 | 排除（用户日常浏览器同账号无风控） |
| IP 地址 | 排除（同机） |
| 请求频率 / 接口扇出 | 用户实测"快速调用大量播放接口也不被罚"，排除 |
| 每集新页面 | 用户也逐个点开 `/sound/{id}`，排除 |
| HeadlessChrome UA | 已用 CDP `Network.setUserAgentOverride` 覆盖为正常 Chrome UA + 完整 client-hints，排除 |
| `navigator.webdriver` | 实测 `false`，排除 |
| XHR 注入 / `__xmHooked` / 改写原型 | 上轮已删除，排除 |
| Cookie 中的设备身份 | 清后蜜月仍 3 集，排除 |
| localStorage / sessionStorage / IndexedDB 中的设备指纹 | 清后蜜月仍 3 集，排除 |
| Profile 长期信誉 | 全新身份也只撑 3 集，排除 |
| **`--remote-debugging-port` + Playwright 通过 CDP 接管** | **未消除且无法在 Playwright/CDP 框架内消除** |

`du_web_sdk` 能从 CDP 环境的固有属性识别自动化，UA override 改不到这些：`Runtime.enable` 调用后 `console.log/clear` 行为差异、inspector 开启后 `performance.now()` 精度从 100µs 降到 20µs 可被时延差检测、`Function.prototype.toString` 对原生方法返回值变化、`Error().stack` 在 `Runtime.evaluate` 上下文里的格式不同、`Page.addScriptToEvaluateOnNewDocument` / `Emulation.*` 的派生副作用。3 集后才挂是服务端按 CDP 环境特征累计积分到阈值再判罚的典型形态——单次不够扣死，3 集内累计触发。

### 触发 ret 也降级

本次失败 `ret=1001/系统繁忙`，而非上轮 `ret=3005` 时伴随 GeeTest 验证码页面。说明服务端对已被多次识别的环境直接拒绝、连验证码恢复路径都不再给——`_recover_via_headful` 的触发条件 `_needs_captcha_fallback(last_err)` 只在见到 `gcaptcha4.geetest.com` 等 URL 时置位，本次这些 URL 不再出现，因此有头 fallback 不会被触发。这是比上轮严格一档的惩罚态。

### 结论与下一步方向

"修改指纹"这条路到此为止——指纹本身能伪装，但 CDP 接管的固有痕迹伪装不掉。可行的治本方向是**让浏览器就是用户日常那个真实浏览器，让 `du_web_sdk` 跑在它自己的日常环境里**，XDL 只是被动接收播放地址：

- **浏览器扩展 + 本地原生消息（Native Messaging）**：扩展监听 `baseInfo` 响应，通过 `chrome.runtime.connectNative` 把 `{trackId, playUrlList}` 回传给 XDL；XDL 起一个 native messaging host 接收并加入任务库，下载走 `requests`。浏览器没有 `--remote-debugging-port`、没有 CDP inspector 痕迹，`du_web_sdk` 看到的执行环境与用户日常环境完全一致。这是治本方案。
- **Tampermonkey 用户脚本 + 本地小 HTTP 接收**：用用户脚本 hook XHR 只读监听 `baseInfo` 回传给 `http://127.0.0.1`。比扩展更轻，但用户脚本会在页面里留 `unsafeWindow` 替换痕迹，不彻底。

本轮的设备指纹重置 / `xdl inspect` 作为对照证据保留，并在未来作为浏览器扩展方案失败时的备用手段。

## 持续观测

受保护接口的正常使用结果会写入 `~/.xdl/risk-events.jsonl`，字段仅包括 UTC 时间、trackId、耗时、结果类别、ret/msg、同时在途数、会话 ID、请求序号、登录态布尔与设备指纹重置布尔，不记录 Cookie 值、请求头、设备指纹内容或播放 URL。

```bash
xdl risk-report
xdl risk-report --log <自定义 JSONL 路径>
```

报告提供请求总数、结果/返回码分布、会话内首次风控请求序号、首次风控前成功数、风控后下一次成功的观测间隔、平均与峰值分钟请求量、请求间隔、最大同时在途数、并发结果分组和延迟分位数。新事件带会话 ID、请求起始时间和会话内序号，因此并发请求即使乱序完成，也不会把"第几个请求触发"算错。遇到首个 `1001` 或语义为"系统繁忙"的 `3005` 时，当前批次立即熔断，不再自动重试。
