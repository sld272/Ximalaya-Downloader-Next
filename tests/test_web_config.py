# -*- coding: utf-8 -*-
import json

import pytest

from xdl.errors import ConfigError
from xdl.frontends.web_config import load_web_settings, save_web_settings
from xdl.settings import Settings


def test_web_settings_round_trip(tmp_path):
    path = tmp_path / "webui.json"
    settings = Settings(
        download_dir=str(tmp_path / "audio"),
        default_quality="high",
        max_concurrency=3,
        source_backend="chrome",
    )

    assert save_web_settings(settings, str(path)) == str(path)
    loaded = load_web_settings(str(path))

    assert loaded.download_dir == str(tmp_path / "audio")
    assert loaded.default_quality == "high"
    assert loaded.max_concurrency == 3
    assert loaded.source_backend == "chrome"


def test_web_settings_ignore_future_unknown_fields(tmp_path):
    path = tmp_path / "webui.json"
    path.write_text(json.dumps({
        "download_dir": "audio",
        "future_option": True,
    }), encoding="utf-8")

    assert load_web_settings(str(path)).download_dir == "audio"


def test_web_settings_reject_invalid_json(tmp_path):
    path = tmp_path / "webui.json"
    path.write_text("[", encoding="utf-8")

    with pytest.raises(ConfigError, match="设置不可读"):
        load_web_settings(str(path))
