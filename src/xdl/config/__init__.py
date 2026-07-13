# -*- coding: utf-8 -*-
"""平台配置包。

子模块按需导入，避免导入 ``xdl.config`` 时提前加载所有配置并形成环依赖。
"""

__all__ = ["paths", "platform", "sign"]
