# -*- coding: utf-8 -*-
"""WebUI 运行设置的本地持久化。"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, fields

from ..config.paths import xdl_home
from ..errors import ConfigError
from ..settings import Settings


_SETTING_NAMES = {field.name for field in fields(Settings)}


def default_settings_path() -> str:
    return os.path.join(xdl_home(), "webui-settings.json")


def load_web_settings(path: str | None = None) -> Settings:
    target = path or default_settings_path()
    if not os.path.exists(target):
        return Settings()
    try:
        with open(target, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"WebUI 设置不可读: {target}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"WebUI 设置必须是 JSON 对象: {target}")
    values = {key: value for key, value in raw.items()
              if key in _SETTING_NAMES}
    try:
        return Settings(**values)
    except (TypeError, ValueError, ConfigError) as exc:
        raise ConfigError(f"WebUI 设置无效: {target}: {exc}") from exc


def save_web_settings(settings: Settings, path: str | None = None) -> str:
    target = path or default_settings_path()
    directory = os.path.dirname(os.path.abspath(target))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".webui-settings-", suffix=".tmp", dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return target


def settings_dict(settings: Settings) -> dict:
    return asdict(settings)
