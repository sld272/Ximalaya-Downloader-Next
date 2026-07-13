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
    print(f"登录态已保存到专用 Chrome 配置目录: {path}")
    return 0


def _cmd_track(app: Facade, args) -> int:
    path = app.download_track(args.target, quality=args.quality,
                              reporter=ConsoleProgress())
    print(f"已保存: {path}")
    return 0


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
            signer.invalidate_cache()
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
                        help="在线音源后端：chrome（默认，CDP 接管）/ http（纯 HTTP + xm-sign）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="打开浏览器登录并保存会话")
    sub.add_parser("resume", help="继续上次未完成的下载")
    p_risk = sub.add_parser("risk-report", help="汇总本地风控观测（不发网络请求）")
    p_risk.add_argument("--log", help="JSONL 观测文件路径")
    sub.add_parser("inspect", help="诊断：列出 Profile 的设备标识存储 key（不读 value）")

    p_track = sub.add_parser("track", help="下载单个音频")
    p_track.add_argument("target", help="音频链接或 trackId")
    p_track.add_argument("--quality", choices=["high", "standard", "low"],
                         help="音质（默认 standard，缺失时自动回退）")

    p_album = sub.add_parser("album", help="顺序批量下载整张专辑")
    p_album.add_argument("target", help="专辑链接或 albumId")
    p_album.add_argument("--quality", choices=["high", "standard", "low"],
                         help="音质（默认 standard，缺失时自动回退）")
    p_album.add_argument("--range", dest="range", metavar="区间",
                         help="下载区间，按专辑内序号：1-20 / 5- / -10 / 7（默认全部）")

    # 纯算 xm-sign 相关调试子命令
    p_sign = sub.add_parser("gen-sign", help="生成 xm-sign（不发受保护请求，仅冒烟测试）")
    p_sign.add_argument("--device-info", dest="device_info",
                        help="设备指纹 JSON 路径（默认 ~/.xdl/device-info.json，不存在用内置模板）")
    p_sign.add_argument("-n", "--repeat", type=int, default=1,
                        help="重复生成次数（默认 1，调试时可用 3 看是否稳定）")

    p_extract = sub.add_parser(
        "extract-device", help="从 Chrome Profile 提取 du_web_sdk 设备指纹到 JSON")
    p_extract.add_argument("-o", "--output", help="输出路径（默认 ~/.xdl/device-info.json）")
    p_extract.add_argument("--profile", help="Chrome 用户目录（默认 ~/.xdl/chrome-profile）")
    p_extract.add_argument("--no-headless", action="store_true",
                           help="显示浏览器窗口（调试可见 SDK 加载过程）")

    p_cookies = sub.add_parser(
        "refresh-cookies", help="从 Chrome Profile 重新提取登录 Cookie 并缓存")
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
    app = Facade.from_config(settings)

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


if __name__ == "__main__":
    sys.exit(main())
