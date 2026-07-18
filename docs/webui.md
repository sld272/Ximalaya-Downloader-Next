# WebUI 使用与接口

WebUI 是 XDL 的本机图形入口，复用 CLI 的同一套 `Settings`、`Facade`、任务库和下载目录，不维护第二份业务状态。

## 启动

```bash
xdl web
# 等价入口
xdl-web
```

默认地址为 `http://127.0.0.1:8787`，启动后自动打开浏览器。可用 `--no-open` 禁止自动打开，或用 `--port` 修改端口。

WebUI 没有远程访问认证，默认只监听回环地址。请勿用 `--host 0.0.0.0` 直接暴露到公网。

## 功能

- 通过专用 Chrome Profile 登录，并显示当前登录态。
- 下载单曲、整张专辑或指定序号区间，选择 `high`、`standard`、`low` 音质。
- 从 SQLite 展示进行中、待恢复、完成和失败任务，支持状态筛选、搜索和打开对应目录。
- 恢复未完成任务；运行中的下载可请求优雅停止，进度保留到任务库和 `.part` 文件。
- 查看完全离线的风控报告，探测曲目可用格式。
- 刷新登录 Cookie、检查浏览器存储 key、生成签名和采集设备信息。
- 编辑下载、重试、路径、Chrome 和实验功能设置。

所有长操作共用一个运行槽。已有操作执行时，新的下载或诊断请求会返回冲突提示；设置也只能在空闲时保存。这一约束与底层音源会话和任务库的生命周期一致。

实验功能区可设置换身冷却时间 `experiment_risk_cooldown_seconds`（默认 15 秒，设为 0 表示不等待），以及换身采集是否使用无头浏览器 `experiment_rotate_headless`（默认关闭，即强制显示浏览器窗口）。这两项只影响已开启的“风控后尝试刷新设备身份”实验，不改变登录或内容授权。

## JSON API

交互式接口文档位于 `/api/docs`。主要端点如下：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/bootstrap` | 设置、登录态、任务和当前操作的首屏快照 |
| `GET` | `/api/tasks` | SQLite 任务列表与状态计数 |
| `GET` | `/api/operation` | 当前/最近一次长操作、日志和结果 |
| `GET` | `/api/risk-report` | 本地风控日志汇总 |
| `POST` | `/api/operations/login` | 启动交互登录 |
| `POST` | `/api/operations/download` | 启动单曲或专辑下载 |
| `POST` | `/api/operations/resume` | 恢复未完成任务 |
| `POST` | `/api/operations/stop` | 请求当前下载优雅停止 |
| `POST` | `/api/operations/formats` | 探测曲目音质格式 |
| `POST` | `/api/operations/gen-sign` | 生成签名用于本地链路检查 |
| `POST` | `/api/operations/refresh-cookies` | 从 Profile 刷新已登录 Cookie |
| `POST` | `/api/operations/inspect-storage` | 列出设备标识相关 storage key |
| `POST` | `/api/operations/extract-device` | 采集设备信息 |
| `PUT` | `/api/settings` | 校验、保存设置并重建运行器 |

长操作返回 `202 Accepted`，前端通过 `/api/operation` 与 `/api/tasks` 获取后续状态。业务错误会返回结构化 `detail`；已有操作占用运行槽时返回 `409 Conflict`。

## 本地数据

WebUI 使用与 CLI 相同的 `~/.xdl` 数据目录，也支持 `XDL_HOME` 覆盖。设置写入 `webui-settings.json`；Cookie、任务、风控日志等路径和敏感性说明见项目 README。前端不会把 Cookie、播放 URL 或设备信息内容返回给浏览器。
