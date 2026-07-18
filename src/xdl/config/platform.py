# -*- coding: utf-8 -*-
"""与平台强相关的数据化配置（见 docs/architecture.md §9）。

这里集中存放最易随平台变动的常量：接口地址、UA、解码置换表/密钥、
注入页面的脚本等。平台一变，原则上只改这个文件 + 解码适配器。
"""
import os
import shutil
import sys

BASE = "https://www.ximalaya.com"
HOME_URL = BASE + "/"
SOUND_URL = BASE + "/sound/{track_id}"
ALBUM_URL = BASE + "/album/{album_id}"

# 专辑曲目清单接口。注意用「非 v1」版：它免签名、可匿名翻全部页（每页 30 条）；
# 而 /revision/album/v1/getTracksList 反而要 webtk 签名且对自动化环境做风控
# （返回「当前环境异常」）。本接口仅给曲目元信息（id/标题/序号），不含 playUrl。
TRACKS_LIST_URL = BASE + "/revision/album/getTracksList"
TRACKS_PAGE_SIZE = 30   # 接口固定每页 30 条（传 pageSize 无效）

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

REFERER = BASE + "/"

# 启动真实 Chrome 的参数（仅开调试端口、不带任何自动化标志，避免被风控识别为机器人）。
# 关键：必须自己干净启动 Chrome 再用 CDP 接管；若让 Playwright 直接 launch，会带上
# --enable-automation 等痕迹，baseInfo 会被 du_web_sdk 风控返回 1001/3005「系统繁忙」。
CHROME_LAUNCH_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
]


def find_chrome() -> str | None:
    """探测本机 Google Chrome（或 Chromium）可执行文件路径。"""
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    else:  # linux
        for name in ("google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None

# ---- www2/mweb2 音频 URL 解密所需的置换表与密钥 ----
PERMUTATION_TABLE_O = [
    183, 174, 108, 16, 131, 159, 250, 5, 239, 110, 193, 202, 153, 137, 251, 176,
    119, 150, 47, 204, 97, 237, 1, 71, 177, 42, 88, 218, 166, 82, 87, 94,
    14, 195, 69, 127, 215, 240, 225, 197, 238, 142, 123, 44, 219, 50, 190, 29,
    181, 186, 169, 98, 139, 185, 152, 13, 141, 76, 6, 157, 200, 132, 182, 49,
    20, 116, 136, 43, 155, 194, 101, 231, 162, 242, 151, 213, 53, 60, 26, 134,
    211, 56, 28, 223, 107, 161, 199, 15, 229, 61, 96, 41, 66, 158, 254, 21,
    165, 253, 103, 89, 3, 168, 40, 246, 81, 95, 58, 31, 172, 78, 99, 45,
    148, 187, 222, 124, 55, 203, 235, 64, 68, 149, 180, 35, 113, 207, 118, 111,
    91, 38, 247, 214, 7, 212, 209, 189, 241, 18, 115, 173, 25, 236, 121, 249,
    75, 57, 216, 10, 175, 112, 234, 164, 70, 206, 198, 255, 140, 230, 12, 32,
    83, 46, 245, 0, 62, 227, 72, 191, 156, 138, 248, 114, 220, 90, 84, 170,
    128, 19, 24, 122, 146, 80, 39, 37, 8, 34, 22, 11, 93, 130, 63, 154,
    244, 160, 144, 79, 23, 133, 92, 54, 102, 210, 65, 67, 27, 196, 201, 106,
    143, 52, 74, 100, 217, 179, 48, 233, 126, 117, 184, 226, 85, 171, 167, 86,
    2, 147, 17, 135, 228, 252, 105, 30, 192, 129, 178, 120, 36, 145, 51, 163,
    77, 205, 73, 4, 188, 125, 232, 33, 243, 109, 224, 104, 208, 221, 59, 9,
]

XOR_KEY_A = [
    204, 53, 135, 197, 39, 73, 58, 160, 79, 24, 12, 83, 180, 250, 101, 60,
    206, 30, 10, 227, 36, 95, 161, 16, 135, 150, 235, 116, 242, 116, 165, 171,
]
