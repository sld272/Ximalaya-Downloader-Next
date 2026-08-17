"""APK 身份状态：与浏览器 Profile/Cookie 完全隔离。"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from threading import RLock

from ...errors import StorageError


class ApkStateStore:
    def __init__(self, state_dir: str):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._device = self._load_or_create_device()

    @property
    def device_id(self) -> str:
        return self._device["deviceId"]

    def _read(self, name: str) -> dict:
        try:
            value = json.loads((self.root / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return {}

    def _atomic_write(self, name: str, value: dict, *, private: bool = False) -> None:
        path = self.root / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if private:
                temporary.chmod(0o600)
            os.replace(temporary, path)
        except OSError as exc:
            raise StorageError(f"无法保存 APK 状态 {path}: {exc}") from exc

    def _load_or_create_device(self) -> dict[str, str]:
        value = self._read("device.json")
        if re.fullmatch(r"[0-9a-fA-F-]{32,36}", str(value.get("deviceId", ""))):
            return {"deviceId": str(value["deviceId"]),
                    "createdAt": str(value.get("createdAt", ""))}
        value = {"deviceId": str(uuid.uuid4()), "createdAt": str(int(time.time()))}
        self._atomic_write("device.json", value)
        return value

    def load_auth(self) -> tuple[str, str]:
        with self._lock:
            value = self._load_accounts()
            active_uid = str(value.get("activeUid", ""))
            account = value.get("accounts", {}).get(active_uid, {})
            token = str(account.get("token", ""))
            return (active_uid, token) if active_uid.isdigit() and token else ("", "")

    def list_accounts(self) -> list[dict]:
        with self._lock:
            value = self._load_accounts()
            active_uid = str(value.get("activeUid", ""))
            accounts = value.get("accounts", {})
            rows = [
                {
                    "uid": uid,
                    "active": uid == active_uid,
                    "saved_at": int(account.get("savedAt") or 0),
                }
                for uid, account in accounts.items()
                if uid.isdigit() and isinstance(account, dict) and account.get("token")
            ]
            return sorted(
                rows,
                key=lambda row: (not row["active"], -row["saved_at"], row["uid"]),
            )

    def save_auth(self, uid: str, token: str) -> None:
        if not uid.isdigit() or not token:
            raise StorageError("APK 登录态缺少有效 uid/token。")
        with self._lock:
            value = self._load_accounts()
            accounts = value.setdefault("accounts", {})
            accounts[uid] = {
                "uid": uid, "token": token, "savedAt": int(time.time()),
            }
            value["activeUid"] = uid
            self._save_accounts(value)

    def switch_auth(self, uid: str) -> tuple[str, str]:
        with self._lock:
            value = self._load_accounts()
            account = value.get("accounts", {}).get(uid, {})
            token = str(account.get("token", ""))
            if not uid.isdigit() or not token:
                raise StorageError(f"APK 账号不存在或登录态无效: {uid}")
            value["activeUid"] = uid
            self._save_accounts(value)
            return uid, token

    def delete_auth(self, uid: str) -> tuple[str, str]:
        with self._lock:
            value = self._load_accounts()
            accounts = value.get("accounts", {})
            if uid not in accounts:
                raise StorageError(f"APK 账号不存在: {uid}")
            del accounts[uid]
            if value.get("activeUid") == uid:
                remaining = sorted(
                    accounts.items(),
                    key=lambda item: int(item[1].get("savedAt") or 0),
                    reverse=True,
                )
                value["activeUid"] = remaining[0][0] if remaining else ""
            self._save_accounts(value)
            active_uid = str(value.get("activeUid", ""))
            token = str(accounts.get(active_uid, {}).get("token", ""))
            return active_uid, token

    def clear_auth(self) -> None:
        with self._lock:
            self._save_accounts({"version": 1, "activeUid": "", "accounts": {}})

    def _load_accounts(self) -> dict:
        value = self._read("accounts.json")
        accounts = value.get("accounts")
        if isinstance(accounts, dict):
            return {
                "version": 1,
                "activeUid": str(value.get("activeUid", "")),
                "accounts": accounts,
            }
        legacy = self._read("auth.json")
        uid, token = str(legacy.get("uid", "")), str(legacy.get("token", ""))
        migrated = {"version": 1, "activeUid": "", "accounts": {}}
        if uid.isdigit() and token:
            migrated["activeUid"] = uid
            migrated["accounts"][uid] = {
                "uid": uid,
                "token": token,
                "savedAt": int(legacy.get("savedAt") or time.time()),
            }
        self._save_accounts(migrated)
        legacy_path = self.root / "auth.json"
        try:
            legacy_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageError(f"无法清理旧 APK 登录态 {legacy_path}: {exc}") from exc
        return migrated

    def _save_accounts(self, value: dict) -> None:
        self._atomic_write("accounts.json", value, private=True)

    def load_xuid(self) -> str:
        value = self._read("xuid.json")
        if value.get("deviceId") == self.device_id and value.get("xuid"):
            return str(value["xuid"])
        return ""

    def save_xuid(self, xuid: str) -> None:
        if not xuid:
            raise StorageError("native signer 返回了空 XUID。")
        self._atomic_write(
            "xuid.json", {"deviceId": self.device_id, "xuid": xuid}, private=True
        )
