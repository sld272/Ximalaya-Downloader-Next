# 项目现状与范围

Ximalaya-Downloader-Next 是一个面向个人授权内容的喜马拉雅音频下载工具，提供 CLI、Textual 终端面板和 Python API。项目当前处于开发阶段，默认使用本地 Python `xm-sign` + HTTP 音源链路。

安装和命令用法见 [README](../README.md)，内部结构见 [架构设计](./architecture.md)。

## 当前主流程

### 首次登录

```text
xdl login
  → 打开专用 Chrome
  → 用户完成登录
  → CDP 仅用于确认 token 已存在
  → 正常关闭 Chrome 并验证 Cookie 已落盘
  → 自动导出到 ~/.xdl/cookies.json
```

登录流程不会把“用户按了回车”当作成功。只有专用 Profile 中的登录 Cookie 已确认持久化，命令才会成功返回。

### 下载

```text
CLI / TUI / Python API
  → Facade
  → 下载用例与任务引擎
  → HttpSource
      ├─ PySignProvider：本地组装载荷并取得 cadd/sid
      ├─ Cookie 缓存：提供登录态
      └─ baseInfo：取得播放信息
  → Www2Decoder
  → FileSink + SqliteTaskStore
```

下载阶段命中新鲜 Cookie 缓存时不会启动 Chrome；缓存失效时会短暂打开专用 Profile 读取已持久化 Cookie，但不会用 CDP 获取播放信息。专辑清单走公开、免签的非 v1 接口；逐集播放信息才使用登录 Cookie 和 `xm-sign`。

## 已实现能力

- 单曲、整张专辑和区间下载。
- `high`、`standard`、`low` 音质选择与缺失回退。
- 文件存在跳过、`.part` 字节级续传、SQLite 任务级恢复。
- `Ctrl-C` / TUI 停止按钮触发优雅停止。
- 网络、签名、鉴权、API、风控和存储错误分类。
- 有界任务调度、失败退避和失败收尾轮。
- 最小化风控事件记录与离线 `risk-report`。
- 本地 Python `xm-sign` 实现及离线契约测试。
- Chrome/CDP 音源兼容后端。

## 明确限制

- `xm-sign` 不是登录 token，也不代表内容授权。
- 签名端点和 `baseInfo` 都是平台相关接口，可能随时变化。
- 当前自动化测试不会向真实平台发请求，因此只能证明本地算法、载荷和解析契约，不能证明线上持续可用。
- 历史 CDP 音源在特定环境下出现过验证码以及 `1001` / `3005`；它已降级为兼容路径。
- 当前没有搜索、桌面 GUI、单文件可执行程序或自动更新。

## 仓库结构

```text
src/xdl/
├─ domain/               领域模型、音质、区间与命名规则
├─ ports/                Source、SignProvider、Decoder、Sink、Store 等协议
├─ application/          Facade、下载/恢复用例与重试调度
├─ adapters/
│  ├─ sign/              xm-sign、Cookie 与设备信息适配器
│  ├─ source_http.py     默认 HTTP 音源
│  ├─ source_chrome.py   Chrome/CDP 兼容音源与登录实现
│  ├─ sink_file.py       文件下载与续传
│  └─ store_sqlite.py    任务持久化
├─ config/               平台常量、签名常量和用户数据路径
├─ frontends/            CLI 与 TUI
├─ composition.py        装配根
├─ risk.py               风控事件与离线汇总
└─ settings.py           运行设置
```

## 下一阶段

优先级按合并后的真实维护价值排序：

1. 用低频、授权的真实样本验证默认 HTTP 后端，并把结果记录为环境相关证据。
2. 把目前保留的高级诊断与无效设备重置实验迁出普通运行路径，经过弃用周期后再删除公开入口。
3. 消除 `Facade.from_config` 与装配根之间的延迟导入环，并补充静态类型检查。
4. 为发布增加 CI、覆盖率报告、锁定的依赖测试矩阵和可安装包验证。
5. 再评估搜索、增量同步和桌面 GUI；在主下载链路稳定前不提前扩张功能面。
