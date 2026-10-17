"""Merge task data through a JSON file stored in a synced shared folder."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from tasks import TaskStore

SYNC_FORMAT_VERSION = 1


class SyncService:
    """Synchronize one account's task records through a cloud-synced file."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path).expanduser()
        if not self.file_path.name:
            raise ValueError("Укажите путь к файлу синхронизации.")

    def synchronize(self, username: str, store: TaskStore) -> int:
        remote_data = self._read()
        accounts = remote_data["users"]
        remote_records = accounts.get(username, [])
        changed = store.merge_sync_records(remote_records)
        local_records = store.export_sync_records()
        if username not in accounts or remote_records != local_records or not self.file_path.exists():
            accounts[username] = local_records
            self._write(remote_data)
        return changed

    def _read(self) -> dict:
        if not self.file_path.exists():
            return {"version": SYNC_FORMAT_VERSION, "users": {}}
        try:
            with self.file_path.open("r", encoding="utf-8") as sync_file:
                data = json.load(sync_file)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Не удалось прочитать файл синхронизации: {error}") from error
        if (
            not isinstance(data, dict)
            or data.get("version") != SYNC_FORMAT_VERSION
            or not isinstance(data.get("users"), dict)
        ):
            raise ValueError("Файл синхронизации поврежден или имеет неподдерживаемый формат.")
        for username, records in data["users"].items():
            if not isinstance(username, str) or not isinstance(records, list):
                raise ValueError("Файл синхронизации содержит данные неверного формата.")
        return data

    def _write(self, data: dict) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.file_path.name}.",
                suffix=".tmp",
                dir=self.file_path.parent,
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as sync_file:
                    json.dump(data, sync_file, ensure_ascii=False, indent=2)
                    sync_file.write("\n")
                    sync_file.flush()
                    os.fsync(sync_file.fileno())
                os.replace(temporary_name, self.file_path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        except OSError as error:
            raise ValueError(f"Не удалось сохранить файл синхронизации: {error}") from error
