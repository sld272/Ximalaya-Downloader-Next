# -*- coding: utf-8 -*-
from fastapi.testclient import TestClient

from xdl.frontends.web import create_app
from xdl.frontends.web_runtime import OperationBusyError


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.busy = False

    def shutdown(self):
        self.calls.append(("shutdown",))

    def bootstrap(self):
        return {
            "settings": {"default_quality": "standard"},
            "login": {"authenticated": False},
            "operation": None,
            "tasks": [],
            "counts": {"all": 0},
            "task_error": None,
        }

    def operation_snapshot(self):
        return None

    def tasks_snapshot(self):
        return {"tasks": [], "counts": {"all": 0}}

    def risk_report(self):
        return {"path": "risk.jsonl", "summary": {"total": 0}}

    def start_download(self, **kwargs):
        if self.busy:
            raise OperationBusyError("已有任务")
        self.calls.append(("download", kwargs))
        return {"id": "1", "status": "running"}

    def start_login(self):
        return {"id": "2", "status": "running"}

    def start_resume(self):
        return {"id": "3", "status": "running"}

    def start_formats(self, target):
        return {"id": "4", "target": target}

    def start_inspect_storage(self):
        return {"id": "5"}

    def start_gen_sign(self, **kwargs):
        return {"id": "6", **kwargs}

    def start_extract_device(self, **kwargs):
        return {"id": "7", **kwargs}

    def start_refresh_cookies(self, **kwargs):
        return {"id": "8", **kwargs}

    def request_stop(self):
        return {"status": "running", "stop_requested": True}

    def update_settings(self, changes):
        self.calls.append(("settings", changes))
        return changes

    def open_downloads(self, task_id=None):
        return {"path": "/tmp/downloads", "task_id": task_id}


def test_web_api_bootstrap_and_download_contract():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/api/bootstrap").json()["counts"] == {"all": 0}

        response = client.post("/api/operations/download", json={
            "mode": "album",
            "target": "https://www.ximalaya.com/album/22",
            "quality": "high",
            "range": "1-20",
        })

    assert response.status_code == 202
    assert runtime.calls[0][0] == "download"
    assert runtime.calls[0][1]["range_"] == "1-20"
    assert runtime.calls[-1] == ("shutdown",)


def test_web_api_rejects_invalid_download_shape():
    with TestClient(create_app(FakeRuntime())) as client:
        response = client.post("/api/operations/download", json={
            "mode": "playlist", "target": "22",
        })

    assert response.status_code == 422


def test_web_api_returns_conflict_for_busy_runtime():
    runtime = FakeRuntime()
    runtime.busy = True
    with TestClient(create_app(runtime)) as client:
        response = client.post("/api/operations/download", json={
            "mode": "track", "target": "11",
        })

    assert response.status_code == 409
    assert response.json() == {"detail": "已有任务"}


def test_web_api_updates_settings_and_opens_task_directory():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        settings = client.put("/api/settings", json={
            "download_dir": "/tmp/audio",
            "max_concurrency": 2,
        })
        opened = client.post("/api/open-downloads", json={"task_id": 9})

    assert settings.status_code == 200
    assert settings.json()["settings"]["max_concurrency"] == 2
    assert opened.json()["task_id"] == 9
