# -*- coding: utf-8 -*-
"""命令行前端（薄壳，见 docs/architecture.md §11）。

只负责参数解析与进度展示（实现 ProgressReporter），业务全部走 Facade。
"""
from __future__ import annotations

import argparse
import sys

from ..application import Facade
from ..application.diagnostics import (extract_device_identity,
                                       generate_signatures,
                                       refresh_login_cookies)
from ..config import paths, platform
from ..settings import Settings
from ..errors import XdlError, CancelledByUser
from ..risk import summarize_risk_events


class ConsoleProgress:
    """控制台进度回报（实现 ProgressReporter 端口）。"""

    @staticmethod
    def _safe_print(s: str, **kw) -> None:
        # Windows 命令行默认 GBK 控制台不能编码 ✓ 等字符；按 errors=replace 兜底，
        # 让"已下载完成"的勾标不被 GBK 编码错中断整批下载。
        try:
            sys.stdout.write(s + kw.get("end", "\n"))
        except UnicodeEncodeError:
            enc = sys.stdout.encoding or "utf-8"
            sys.stdout.buffer.write((s + kw.get("end", "\n")).encode(enc, "replace"))
            sys.stdout.flush()

    def start(self, title: str, total: int) -> None:
        self._title = title
        self._safe_print(f"开始下载: {title}" + (f"  ({total} bytes)" if total else ""))

    def update(self, done: int, total: int) -> None:
        if total:
            pct = done * 100 // total
            self._safe_print(f"\r  {pct:3d}%  ({done}/{total} bytes)", end="")
        else:
            self._safe_print(f"\r  {done} bytes", end="")

    def finish(self, path: str) -> None:
        self._safe_print("")

    def note(self, msg: str) -> None:
        self._safe_print(msg)


def _cmd_login(app: Facade, args) -> int:
    path = app.login()
    settings = getattr(args, "settings", None)
    if settings is not None:
        name = platform.browser_display_name(
            getattr(settings, "resolved_browser", "chrome"))
        print(f"登录成功（浏览器: {name}），登录态已保存: {path}")
        print(f"凭据缓存: {settings.cookies_cache_path}")
    else:
        print(f"登录成功，登录态已保存: {path}")
    _maybe_print_browser_hint(args)
    print("现在可以直接运行 `xdl track`、`xdl album` 或 `xdl resume`。")
    return 0


def _maybe_print_browser_hint(args) -> None:
    """双浏览器机器且未显式选择时提示如何切换（浏览器选择只在登录时与用户相关）。"""
    if getattr(args, "browser", None):
        return
    if platform.find_chrome() and platform.find_edge():
        print("提示：检测到同时安装了 Chrome 与 Edge，当前使用 Chrome；"
              "如需改用 Edge，请加全局参数 `--browser edge`"
              "（每个浏览器的登录态与指纹各自独立保存，互不覆盖）。")


def _cmd_track(app: Facade, args) -> int:
    if args.list_formats:
        return _cmd_list_formats(app, args)
    path = app.download_track(args.target, quality=args.quality,
                              reporter=ConsoleProgress())
    print(f"已保存: {path}")
    return 0


def _cmd_list_formats(app: Facade, args) -> int:
    info = app.list_formats(args.target)
    formats = info["formats"]

    print(f"曲目: {info['title']}")
    print(f"ID: {info['track_id']}")
    print(f"默认音质: {info['default_quality']}")
    print()
    print(f"{'ID':>3s}  {'格式':12s} {'编码':>5s}  {'码率':>6s}  {'文件大小':>10s}")
    print(f"{'---':3s}  {'----------':12s} {'-----':>5s}  {'------':>6s}  {'----------':>10s}")
    for i, f in enumerate(formats):
        bitrate = f"{f['bitrate']}k" if f["bitrate"] > 0 else "?"
        size_str = _fmt_size(f["file_size"])
        print(f"{i:3d}  {f['type']:12s} {f['codec']:>5s}  {bitrate:>6s}  {size_str:>10s}")
    print()
    print(f"共 {len(formats)} 种格式")
    return 0


def _fmt_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "未知"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _cmd_album(app: Facade, args) -> int:
    result = app.download_album(args.target, quality=args.quality,
                                range_=args.range, reporter=ConsoleProgress())
    _print_album_result(result)
    if result.stopped:
        print("\n已优雅停止，`xdl resume` 可继续。", file=sys.stderr)
        return 130
    return 1 if result.failed or getattr(result, "risk_control", None) else 0


def _cmd_resume(app: Facade, args) -> int:
    results = app.resume(reporter=ConsoleProgress())
    if not results:
        return 0
    failed = False
    stopped = False
    for result in results:
        _print_album_result(result)
        failed = (failed or bool(result.failed)
                  or bool(getattr(result, "risk_control", None)))
        stopped = stopped or result.stopped
    if stopped:
        print("\n已优雅停止，`xdl resume` 可继续。", file=sys.stderr)
        return 130
    return 1 if failed else 0


def _cmd_risk_report(app: Facade, args) -> int:
    path = args.log or Settings().risk_log_path
    summary = summarize_risk_events(path)
    print(f"风控观测文件: {path}")
    print(f"总请求: {summary['total']}")
    print(f"结果分布: {summary['outcomes']}")
    print(f"返回码分布: {summary['ret_counts']}")
    print(f"首次风控请求序号: {summary['first_risk_request_index']}")
    print(f"首次风控前成功数: {summary['successes_before_first_risk']}")
    print(f"观测到的恢复时间(秒): {summary['recovery_seconds']}")
    print(f"观测跨度(秒): {summary['duration_seconds']}")
    print(f"平均请求速度(次/分钟): {summary['requests_per_minute']}")
    print(f"峰值一分钟请求量: {summary['peak_requests_per_minute']}")
    print(f"请求间隔(秒): {summary['request_interval_seconds']}")
    print(f"最大同时在途: {summary['max_in_flight']}")
    print(f"并发分组: {summary['outcomes_by_in_flight']}")
    print(f"登录态分组: {summary['outcomes_by_authentication']}")
    print(f"最新会话: {summary['latest_session']}")
    print(f"延迟(ms): {summary['latency_ms']}")
    return 0


def _cmd_inspect(app: Facade, args) -> int:
    import json
    report = app.inspect_storage()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _cmd_gen_sign(app: Facade, args) -> int:
    """纯算 xm-sign 冒烟：调用 PySignProvider 生成 xm-sign 并打印。"""
    result = generate_signatures(args.device_info, args.repeat)
    for i, value in enumerate(result["values"]):
        print(f"[{i + 1}/{result['repeat']}] xm-sign: {value}")
    return 0


def _cmd_extract_device(app: Facade, args) -> int:
    """从浏览器专用 Profile 提取 du_web_sdk 设备指纹到 JSON 文件。"""
    settings = Settings(browser=getattr(args, "browser", None) or "auto")
    result = extract_device_identity(
        settings,
        output=args.output,
        profile=args.profile,
        headless=not args.no_headless,
        refresh=bool(getattr(args, "refresh", False)),
        fresh_profile=bool(getattr(args, "fresh_profile", False)),
    )
    print(f"已保存 {result['field_count']} 个字段到 {result['output_path']}")
    print(f"identity={result['identity']}")
    print(result["summary"])
    return 0


def _cmd_refresh_cookies(app: Facade, args) -> int:
    """从浏览器专用 Profile 重新提取登录 Cookie 到 ~/.xdl/cookies.json。"""
    settings = Settings(browser=getattr(args, "browser", None) or "auto")
    result = refresh_login_cookies(settings, headless=not args.no_headless)
    print(f"已保存 {result['cookie_count']} 个 Cookie 到 "
          f"{result['output_path']}（已登录）")
    return 0


def _cmd_web(app: Facade, args) -> int:
    """启动本地 WebUI；由 Web 运行器自行装配 Facade。"""
    from .web import serve
    return serve(host=args.host, port=args.port,
                 open_browser=not args.no_open)


def _print_album_result(result) -> None:
    print("\n" + result.summary())
    if result.failed:
        print("失败明细：")
        for at, err in result.failed:
            print(f"  [{at.index}] {at.title} — {err}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xdl", description="喜马拉雅音频下载器")
    parser.add_argument("--download-dir", help="下载目录（默认 ./downloads）")
    parser.add_argument("--browser", choices=["auto", "chrome", "edge"],
                        help="登录/采集所用浏览器：auto（默认，Chrome 优先、Edge 兜底）")
    parser.add_argument("--source-backend", choices=["chrome", "http", "pc", "apk"],
                        help="在线音源后端：http（默认，本地 xm-sign）/ "
                             "chrome（兼容回退）/ apk（Android APK 协议）")
    parser.add_argument(
        "--concurrency", type=_positive_int, metavar="N",
        help="专辑下载/恢复的异步并发数（默认 1；提高可能触发平台风控）",
    )
    parser.add_argument(
        "--experiment-rotate-device",
        action="store_true",
        help="[实验] 命中风控后用浏览器刷新设备指纹并重试（默认关闭；不保证有效）",
    )
    parser.add_argument(
        "--experiment-risk-cooldown",
        type=float,
        metavar="SEC",
        help="[实验] 风控后换身/探针前冷却秒数（默认 15；0=不等待）",
    )
    parser.add_argument(
        "--experiment-rotate-headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[实验] 换身是否无头（默认有头；--experiment-rotate-headless 强制无头）",
    )
    parser.add_argument(
        "--experiment-strip-device-cookies",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[实验] 是否剥离设备 Cookie（默认保留，与新 device_info 成套；"
             "--experiment-strip-device-cookies 强制剥离）",
    )
    parser.add_argument(
        "--experiment-require-identity-change",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[实验] 换身是否要求 session/hardware 身份字段变化（默认要求）",
    )
    parser.add_argument(
        "--experiment-rebirth-rounds",
        type=_positive_int,
        metavar="N",
        help="[实验] 清 storage 后的重生轮数（默认 2）",
    )
    parser.add_argument(
        "--risk-poll",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="[实验] 风控后自动轮询等待解除并继续下载（默认关闭；"
             "--risk-poll 开启，--no-risk-poll 显式关闭）",
    )
    parser.add_argument(
        "--risk-poll-initial-wait",
        type=float,
        metavar="SEC",
        help="[实验] 风控轮询首次等待秒数（默认 30；0=立即探测）",
    )
    parser.add_argument(
        "--risk-poll-max-duration",
        type=float,
        metavar="SEC",
        help="[实验] 风控轮询总等待上限秒数（默认 3600；0=不限）",
    )
    sub = parser.add_subparsers(
        dest="command", required=True,
        metavar="{web,login,track,album,resume,gen-sign,risk-report}",
    )

    p_web = sub.add_parser("web", help="启动本地 WebUI")
    p_web.add_argument("--host", default="127.0.0.1",
                       help="监听地址（默认 127.0.0.1）")
    p_web.add_argument("--port", type=_port, default=8787,
                       help="监听端口（默认 8787）")
    p_web.add_argument("--no-open", action="store_true",
                       help="启动后不自动打开浏览器")
    sub.add_parser("login", help="打开浏览器登录并保存会话")
    p_track = sub.add_parser("track", help="下载单个音频")
    p_track.add_argument("target", help="音频链接或 trackId")
    p_track.add_argument("--quality", choices=["high", "standard", "low"],
                         help="音质（默认 standard，缺失时自动回退）")
    p_track.add_argument("-F", "--list-formats", action="store_true",
                         help="列出所有可用音质格式（类似 yt-dlp -F）")

    p_album = sub.add_parser("album", help="顺序批量下载整张专辑")
    p_album.add_argument("target", help="专辑链接或 albumId")
    p_album.add_argument("--quality", choices=["high", "standard", "low"],
                         help="音质（默认 standard，缺失时自动回退）")
    p_album.add_argument("--range", dest="range", metavar="区间",
                         help="下载区间，按专辑内序号：1-20 / 5- / -10 / 7（默认全部）")
    sub.add_parser("resume", help="继续上次未完成的下载")

    # 常用诊断：保留在一级帮助中。
    p_sign = sub.add_parser("gen-sign", help="生成 xm-sign（不发受保护请求，仅冒烟测试）")
    p_sign.add_argument("--device-info", dest="device_info",
                        help="设备指纹 JSON 路径（默认 ~/.xdl/device-info.json，不存在用内置模板）")
    p_sign.add_argument("-n", "--repeat", type=_positive_int, default=1,
                        help="重复生成次数（默认 1，调试时可用 3 看是否稳定）")
    p_risk = sub.add_parser("risk-report", help="汇总本地风控观测（不发网络请求）")
    p_risk.add_argument("--log", help="JSONL 观测文件路径")

    # 兼容保留的高级诊断命令：不再挤占主帮助，但原命令仍可调用。
    sub.add_parser("inspect")
    p_extract = sub.add_parser(
        "extract-device")
    p_extract.add_argument("-o", "--output", help="输出路径（默认 ~/.xdl/device-info.json）")
    p_extract.add_argument("--profile", help="浏览器用户目录（默认专用 Profile，"
                                             "如 ~/.xdl/chrome-profile，随 --browser 变化）")
    p_extract.add_argument("--no-headless", action="store_true",
                           help="显示浏览器窗口（调试可见 SDK 加载过程）")
    p_extract.add_argument(
        "--refresh", action="store_true",
        help="先清设备 Cookie/storage，再让 du_web_sdk 重生后采集（真换指纹）",
    )
    p_extract.add_argument(
        "--fresh-profile", action="store_true",
        help="使用全新临时 Profile 采集（完全新设备；通常无登录态）",
    )

    p_cookies = sub.add_parser("refresh-cookies")
    p_cookies.add_argument("--no-headless", action="store_true",
                           help="显示浏览器窗口")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # 旧的浏览器无关缓存（~/.xdl/cookies.json 等）搬到 Chrome 布局，
    # 必须在任何 Settings 派生路径之前完成，否则老用户会被判定为未登录。
    paths.migrate_legacy_layout()
    # browser 参与 Settings.__post_init__ 的路径派生（可执行文件探测、专用
    # Profile 与 Cookie/指纹缓存默认路径），必须在构造时传入，不能事后赋值。
    settings = Settings(browser=args.browser or "auto")
    # 供 login 等命令回显实际使用的浏览器与落盘路径。
    args.settings = settings
    if args.download_dir:
        settings.download_dir = args.download_dir
    if getattr(args, "source_backend", None):
        settings.source_backend = args.source_backend
    if args.concurrency is not None:
        settings.max_concurrency = args.concurrency
    if getattr(args, "experiment_rotate_device", False):
        settings.experiment_rotate_device_on_risk = True
    if getattr(args, "experiment_risk_cooldown", None) is not None:
        settings.experiment_risk_cooldown_seconds = float(args.experiment_risk_cooldown)
    if getattr(args, "experiment_rotate_headless", None) is not None:
        settings.experiment_rotate_headless = bool(args.experiment_rotate_headless)
    if getattr(args, "experiment_strip_device_cookies", None) is not None:
        settings.experiment_strip_device_cookies = bool(
            args.experiment_strip_device_cookies
        )
    if getattr(args, "experiment_require_identity_change", None) is not None:
        settings.experiment_require_identity_change = bool(
            args.experiment_require_identity_change
        )
    if getattr(args, "experiment_rebirth_rounds", None) is not None:
        settings.experiment_rebirth_rounds = int(args.experiment_rebirth_rounds)
    if getattr(args, "risk_poll", None) is not None:
        settings.risk_poll_enabled = bool(args.risk_poll)
    if getattr(args, "risk_poll_initial_wait", None) is not None:
        settings.risk_poll_initial_wait = float(args.risk_poll_initial_wait)
    if getattr(args, "risk_poll_max_duration", None) is not None:
        settings.risk_poll_max_duration = float(args.risk_poll_max_duration)
    handlers = {
        "web": _cmd_web,
        "login": _cmd_login,
        "track": _cmd_track,
        "album": _cmd_album,
        "resume": _cmd_resume,
        "risk-report": _cmd_risk_report,
        "inspect": _cmd_inspect,
        "gen-sign": _cmd_gen_sign,
        "extract-device": _cmd_extract_device,
        "refresh-cookies": _cmd_refresh_cookies,
    }
    try:
        # 本地诊断命令不需要装配下载器，避免无谓初始化 Chrome/任务库/HTTP 后端。
        app = (Facade.from_config(settings)
               if args.command in {"login", "track", "album", "resume", "inspect"}
               else None)
        return handlers[args.command](app, args)
    except CancelledByUser as e:
        print(f"\n{e}", file=sys.stderr)
        return 130
    except XdlError as e:
        print(f"\n[错误] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
    return number


if __name__ == "__main__":
    sys.exit(main())
