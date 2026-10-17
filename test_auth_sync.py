"""Automated tests for local account security and shared-file synchronization."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sync import SyncService
from tasks import AuthStore, TaskStore


class AuthAndSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = self.root / "tasks.db"
        self.auth = AuthStore(self.database)

    def tearDown(self) -> None:
        self.auth.close()
        self.temp_dir.cleanup()

    def test_registration_authentication_and_isolated_task_lists(self) -> None:
        alice = self.auth.register("Alice_1", "a-secure-password")
        bob = self.auth.register("bob_user", "another-secure-password")
        self.assertEqual(self.auth.authenticate("ALICE_1", "a-secure-password"), alice)
        with self.assertRaisesRegex(ValueError, "Неверное"):
            self.auth.authenticate("alice_1", "wrong-password")
        with self.assertRaisesRegex(ValueError, "уже занято"):
            self.auth.register("alice_1", "yet-another-password")
        with self.assertRaisesRegex(ValueError, "не менее 8"):
            self.auth.register("short", "123")

        alice_tasks = TaskStore(self.database, alice.id)
        bob_tasks = TaskStore(self.database, bob.id)
        try:
            alice_tasks.create("Личная задача Алисы")
            bob_tasks.create("Личная задача Боба")
            self.assertEqual(
                [task.title for task in alice_tasks.list_tasks()],
                ["Личная задача Алисы"],
            )
            self.assertEqual(
                [task.title for task in bob_tasks.list_tasks()],
                ["Личная задача Боба"],
            )
        finally:
            alice_tasks.close()
            bob_tasks.close()

    def test_sync_merges_changes_and_propagates_deletions(self) -> None:
        user = self.auth.register("sync_user", "sync-password-123")
        first_device = TaskStore(self.database, user.id)
        second_device = TaskStore(self.root / "second-device.db", user.id)
        service = SyncService(self.root / "cloud" / "planer-sync.json")
        try:
            task = first_device.create(
                "Общая задача", category="Учеба", priority="Высокий", due_date="2026-12-01"
            )
            self.assertEqual(service.synchronize(user.username, first_device), 0)
            self.assertEqual(service.synchronize(user.username, second_device), 1)
            restored = second_device.list_tasks()[0]
            self.assertEqual(restored.sync_id, task.sync_id)
            self.assertEqual(restored.category, "Учеба")

            first_device.update(task.id, "Обновленная задача", priority="Средний")
            service.synchronize(user.username, first_device)
            self.assertEqual(service.synchronize(user.username, second_device), 1)
            self.assertEqual(second_device.list_tasks()[0].title, "Обновленная задача")

            first_device.delete(task.id)
            service.synchronize(user.username, first_device)
            self.assertEqual(service.synchronize(user.username, second_device), 1)
            self.assertEqual(second_device.list_tasks(), [])
            self.assertEqual(len(second_device.export_sync_records()), 1)
            self.assertIsNotNone(second_device.export_sync_records()[0]["deleted_at"])
        finally:
            first_device.close()
            second_device.close()

    def test_sync_preserves_other_account_data_and_rejects_corrupt_file(self) -> None:
        user = self.auth.register("file_user", "file-password-123")
        store = TaskStore(self.database, user.id)
        sync_file = self.root / "sync.json"
        service = SyncService(sync_file)
        try:
            store.create("Синхронизируемая задача")
            sync_file.write_text(
                json.dumps({"version": 1, "users": {"other": []}}),
                encoding="utf-8",
            )
            service.synchronize(user.username, store)
            data = json.loads(sync_file.read_text(encoding="utf-8"))
            self.assertIn("other", data["users"])
            self.assertEqual(len(data["users"][user.username]), 1)
            self.assertNotIn("file-password-123", sync_file.read_text(encoding="utf-8"))

            sync_file.write_text("{broken json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "прочитать файл"):
                service.synchronize(user.username, store)
        finally:
            store.close()

    def test_previous_version_tasks_are_claimed_by_first_profile(self) -> None:
        legacy_database = self.root / "legacy.db"
        connection = sqlite3.connect(legacy_database)
        connection.execute(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Без категории',
                priority TEXT NOT NULL,
                due_date TEXT,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO tasks (title, priority, created_at)
            VALUES (?, ?, ?)
            """,
            ("Задача из прежней версии", "Средний", "2026-10-17T14:00:00"),
        )
        connection.commit()
        connection.close()

        auth = AuthStore(legacy_database)
        user = auth.register("legacy_user", "legacy-password-123")
        store = TaskStore(legacy_database, user.id)
        try:
            self.assertEqual(store.claim_legacy_tasks(), 1)
            restored = store.list_tasks()
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].title, "Задача из прежней версии")
            self.assertTrue(restored[0].sync_id)
            self.assertTrue(restored[0].updated_at.endswith("+00:00"))
        finally:
            store.close()
            auth.close()


if __name__ == "__main__":
    unittest.main()
