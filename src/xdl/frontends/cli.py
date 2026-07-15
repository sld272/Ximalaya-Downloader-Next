# -*- coding: utf-8 -*-
"""命令行前端（薄壳，见 docs/architecture.md §11）。

只负责参数解析与进度展示（实现 ProgressReporter），业务全部走 Facade。
"""
from __future__ import annotations

import argparse
import sys

from ..application import Facade
from ..settings import Settings
from ..errors import AuthError, XdlError, CancelledByUser
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
    print(f"登录成功，登录态已保存: {path}")
    print("现在可以直接运行 `xdl track`、`xdl album` 或 `xdl resume`。")
    return 0


def _cmd_track(app: Facade, args) -> int:
    if args.list_formats:
        return _cmd_list_formats(app, args)
    path = app.download_track(args.target, quality=args.quality,
                              reporter=ConsoleProgress())
    print(f"已保存: {path}")
    return 0


def _cmd_list_formats(app: Facade, args) -> int:
    info = app.list_formats(args.target)
    # 按码率+编码排序，与 Quality.negotiate 一致
    from ..domain.models import _type_score
    formats = sorted(info["formats"], key=lambda f: _type_score(f["type"]),
                     reverse=True)

    print(f"曲目: {info['title']}")
    print(f"ID: {info['track_id']}")
    print(f"默认音质: {info['default_quality']}")
    print()
    print(f"{'ID':>3s}  {'格式':12s} {'编码':>5s}  {'码率':>6s}  {'文件大小':>10s}")
    print(f"{'---':3s}  {'----------':12s} {'-----':>5s}  {'------':>6s}  {'----------':>10s}")
    for i, f in enumerate(formats):
        parts = f["type"].split("_")
        codec = parts[0] if parts else "?"
        bitrate = parts[1] if len(parts) >= 2 else "?"
        size_str = _fmt_size(f["file_size"])
        print(f"{i:3d}  {f['type']:12s} {codec:>5s}  {bitrate + 'k':>6s}  {size_str:>10s}")
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
    return 1 if result.failed else 0


def _cmd_resume(app: Facade, args) -> int:
    results = app.resume(reporter=ConsoleProgress())
    if not results:
        return 0
    failed = False
    stopped = False
    for result in results:
        _print_album_result(result)
        failed = failed or bool(result.failed)
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
    from ..adapters import PySignProvider
    signer = PySignProvider(device_info_path=args.device_info)
    signer.open()
    try:
        for i in range(args.repeat):
            value = signer.sign()
            print(f"[{i + 1}/{args.repeat}] xm-sign: {value}")
    finally:
        signer.close()
    return 0


def _cmd_extract_device(app: Facade, args) -> int:
    """从 Chrome Profile 提取 du_web_sdk 设备指纹到 JSON 文件。"""
    from ..adapters.sign import extract_device_info, save_device_info

    info = extract_device_info(
        profile_dir=args.profile or Settings().chrome_profile_dir,
        chrome_path=Settings().chrome_path,
        headless=not args.no_headless,
    )
    out = args.output or Settings().device_info_path
    save_device_info(info, out)
    print(f"已保存 {len(info)} 个字段到 {out}")
    return 0


def _cmd_refresh_cookies(app: Facade, args) -> int:
    """从 Chrome Profile 重新提取登录 Cookie 到 ~/.xdl/cookies.json。"""
    from ..adapters.sign import (extract_cookies_from_profile, save_cookies,
                                 is_login_cookie)
    settings = Settings()
    cookies = extract_cookies_from_profile(
        profile_dir=settings.chrome_profile_dir,
        chrome_path=settings.chrome_path,
        headless=not args.no_headless,
    )
    if not is_login_cookie(cookies):
        raise AuthError(
            "专用 Chrome Profile 中未发现登录 token（1&_token）；"
            "未覆盖现有 Cookie 缓存。"
        )
    save_cookies(cookies, settings.cookies_cache_path)
    print(f"已保存 {len(cookies)} 个 Cookie 到 {settings.cookies_cache_path}（已登录）")
    return 0


def _print_album_result(result) -> None:
    print("\n" + result.summary())
    if result.failed:
        print("失败明细：")
        for at, err in result.failed:
            print(f"  [{at.index}] {at.title} — {err}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xdl", description="喜马拉雅音频下载器")
    parser.add_argument("--download-dir", help="下载目录（默认 ./downloads）")
    parser.add_argument("--source-backend", choices=["chrome", "http"],
                        help="在线音源后端：http（默认，本地 xm-sign）/ chrome（兼容回退）")
    parser.add_argument(
        "--concurrency", type=_positive_int, metavar="N",
        help="专辑下载/恢复的异步并发数（默认 1；提高可能触发平台风控）",
    )
    sub = parser.add_subparsers(
        dest="command", required=True,
        metavar="{login,track,album,resume,gen-sign,risk-report}",
    )

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
    p_extract.add_argument("--profile", help="Chrome 用户目录（默认 ~/.xdl/chrome-profile）")
    p_extract.add_argument("--no-headless", action="store_true",
                           help="显示浏览器窗口（调试可见 SDK 加载过程）")

    p_cookies = sub.add_parser("refresh-cookies")
    p_cookies.add_argument("--no-headless", action="store_true",
                           help="显示浏览器窗口")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = Settings()
    if args.download_dir:
        settings.download_dir = args.download_dir
    if getattr(args, "source_backend", None):
        settings.source_backend = args.source_backend
    if args.concurrency is not None:
        settings.max_concurrency = args.concurrency
    handlers = {
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


if __name__ == "__main__":
    sys.exit(main())
