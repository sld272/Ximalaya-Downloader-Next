# -*- coding: utf-8 -*-
"""xdl —— 喜马拉雅音频下载器核心库。

公开入口是 Facade：

    from xdl import Facade
    app = Facade.from_config()
    app.download_track("https://www.ximalaya.com/sound/123456")
"""
from .application import Facade
from .settings import Settings
from . import errors

__all__ = ["Facade", "Settings", "errors"]
__version__ = "0.1.0"
