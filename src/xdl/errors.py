# -*- coding: utf-8 -*-
"""类型化异常体系（见 docs/architecture.md §10.1）。

每个异常带 retryable 标志，未来任务引擎据此决定重试策略。
当前 MVP 只用到其中一部分，但先把体系立起来，方便后续接入引擎。
"""


class XdlError(Exception):
    """所有业务异常的基类。"""
    retryable = False
    category = "generic"


class ConfigError(XdlError):
    category = "config"


class AuthError(XdlError):
    """未登录 / 会话过期 / 未授权。"""
    category = "auth"


class SignError(XdlError):
    """签名生成失败，通常可重试。"""
    retryable = True
    category = "sign"


class NetworkError(XdlError):
    """超时 / 连接重置等，可重试。"""
    retryable = True
    category = "network"


class CancelledByUser(XdlError):
    """用户请求优雅停止；任务应保留为可恢复状态。"""
    retryable = True
    category = "cancelled"


class ApiError(XdlError):
    """接口层错误（限流 / 被拒 / 不存在 / 响应异常）。

    携带平台返回的 ret 码，便于上层精细处理（如 1001 限流可重试）。
    """
    category = "api"

    def __init__(self, message: str, ret: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.ret = ret
        self.retryable = retryable


class DecodeError(XdlError):
    category = "decode"


class StorageError(XdlError):
    category = "storage"
