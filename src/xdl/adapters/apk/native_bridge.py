"""Java/Unidbg signer 的长驻 JSON Lines RPC 桥。"""
from __future__ import annotations

import json
import hashlib
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from ...errors import ConfigError, SignError


class ApkNativeBridge:
    def __init__(self, *, java_path: str, signer_jar: str, libcxx: str,
                 login_so: str, xuid_so: str, encrypt_so: str,
                 asset_dir: str,
                 timeout: float = 30.0):
        self.asset_dir = asset_dir
        self.command = [java_path or "java", f"-Dxmly.asset.dir={asset_dir}",
                        "-jar", signer_jar, libcxx,
                        login_so, xuid_so, encrypt_so]
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._responses: queue.Queue[str | None] = queue.Queue()

    def _validate(self) -> None:
        labels = ("signer JAR", "libc++", "login so", "xuid so", "encrypt so")
        missing = [f"{label}: {path}" for label, path in zip(labels, self.command[3:])
                   if not path or not Path(path).is_file()]
        assets = (
            Path(self.asset_dir) / "na.czl",
            Path(self.asset_dir) / "drawable" / "x_m.png",
        )
        for asset in assets:
            if not asset.is_file():
                missing.append(f"asset {asset.relative_to(self.asset_dir)}: {asset}")
        if missing:
            raise ConfigError("APK native 资产缺失：" + "；".join(missing))
        manifest_path = Path(self.command[3]).parent / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                expected = manifest.get("files", {})
                paths = [Path(path) for path in self.command[3:]] + list(assets)
                for path in paths:
                    relative = (
                        f"assets/{path.relative_to(self.asset_dir).as_posix()}"
                        if path in assets else path.name
                    )
                    wanted = expected.get(relative)
                    if wanted and hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
                        raise ConfigError(f"APK native 资产校验失败: {path}")
            except (OSError, ValueError, TypeError) as exc:
                raise ConfigError(f"无法校验 APK native 资产: {exc}") from exc

    def open(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            self._validate()
            try:
                self._process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                )
                self._responses = queue.Queue()
                stdout = self._process.stdout
                threading.Thread(
                    target=self._read_stdout, args=(stdout, self._responses),
                    name="xdl-apk-native-stdout", daemon=True,
                ).start()
                self._request_locked({"op": "ping"})
            except OSError as exc:
                self._process = None
                raise ConfigError(f"无法启动 APK native signer: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._responses = queue.Queue()
            if process is None:
                return
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=min(max(self.timeout, 0.1), 5.0))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def _request_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None or not process.stdin or not process.stdout:
            raise SignError("APK native signer 未运行。")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
            while True:
                try:
                    line = self._responses.get(timeout=max(float(self.timeout), 0.1))
                except queue.Empty as exc:
                    raise SignError(
                        f"APK native signer 响应超时（{self.timeout:g}s）。"
                    ) from exc
                if line is None:
                    raise SignError(f"APK native signer 已退出（exit={process.poll()}）。")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(response, dict) or "ok" not in response:
                    continue
                if not response.get("ok"):
                    raise SignError(str(response.get("error") or "APK native 调用失败。"))
                return response
        except (BrokenPipeError, OSError) as exc:
            raise SignError(f"APK native RPC 失败: {exc}") from exc

    @staticmethod
    def _read_stdout(stdout, responses: queue.Queue[str | None]) -> None:
        if stdout is None:
            responses.put(None)
            return
        try:
            for line in stdout:
                responses.put(line)
        finally:
            responses.put(None)

    def call(self, operation: str, **values: Any) -> Any:
        with self._lock:
            for attempt in range(2):
                try:
                    self.open()
                    return self._request_locked({"op": operation, **values}).get("value")
                except SignError as exc:
                    self.close()
                    if "响应超时" in str(exc):
                        raise
                    if attempt:
                        raise
            raise SignError("APK native 调用失败。")

    def encrypt_mobile(self, mobile: str) -> str:
        return str(self.call("encryptMobile", mobile=mobile))

    def sign(self, values: dict[str, str]) -> str:
        return str(self.call("sign", values=values, production=True))

    def create_xuid(self, stable_id: str) -> str:
        return str(self.call("createXuid", stableId=stable_id))

    def ticket(self, attr: str, xuid: str) -> str:
        return str(self.call("ticket", attr=attr, xuid=xuid))

    def decrypt_download(self, value: str, version: int) -> str:
        return str(self.call("decryptDownload", value=value, version=int(version)))
