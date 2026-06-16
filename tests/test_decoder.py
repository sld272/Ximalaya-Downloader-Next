# -*- coding: utf-8 -*-
"""解码器金标向量测试（见 docs/architecture.md §10.3）。

用一条真实捕获的「加密输入 → 已知解密输出」样本钉死 www2 解码算法；
置换表/密钥若被误改，这条立刻报红。样本中的 token 等仅为历史值，
不影响解码确定性（解码是对输入的纯函数）。
"""
from xdl.adapters import Www2Decoder

# 真实捕获自 trackId=541566924（长度 287，非 4 倍数 —— 专门覆盖 padding 修复）
ENC = ("w871-MnPl16htllm6Sz1xpiP16kn0SNAgjAR8NcW5AvE0IEwSiPlv2bLuGTFKFq4w0udO85DaJ_O-"
       "IJXWB8-BM-cWRjQWQ7blbMruZy97Muxgj7omiPZ4NXVtIQnZHas53L-GFZM2tVDUfVB6eNMA7UILJ"
       "lWvm-6JSXCWTuEdrBfPVH6nDMx1ypqlauSQrZ3L0gY4URa07iW24a6J8bvkrqpP-pKFHH661mVlST"
       "Qe1xk23aZVmZvrgA-v3joeFU9oIXIFahGh-uIjkaGGbFxABFJFlZFdk4")
EXPECTED = ("https://a.xmcdn.com/group24/M07/02/66/wKgJMFgQszjCb8kmAANtUmAoefM142.mp3"
            "?sign=716a2ed9ec440ea2899c22fe3bd181c8&buy_key=www2_968e291c-2184357:"
            "74284569&timestamp=1781610174555000&token=5018&duration=56")


def test_www2_decode_golden_vector():
    assert Www2Decoder().decode(ENC) == EXPECTED


def test_plaintext_passthrough():
    url = "https://aod.cos.tx.xmcdn.com/x.m4a"
    assert Www2Decoder().decode(url) == url


def test_empty():
    assert Www2Decoder().decode("") == ""
