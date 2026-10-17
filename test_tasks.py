"""Automated tests for task creation, editing, filtering, and persistence."""

import tempfile
import unittest
from pathlib import Path

from tasks import TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "tasks.db"
        self.store = TaskStore(self.database)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_create_trims_fields_and_applies_category_default(self) -> None:
        task = self.store.create("  Купить хлеб  ", category="  ", priority="Высокий")

        self.assertEqual(task.title, "Купить хлеб")
        self.assertEqual(task.category, "Без категории")
        self.assertEqual(task.priority, "Высокий")
        self.assertFalse(task.completed)

    def test_rejects_empty_title_and_invalid_due_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "название"):
            self.store.create("  ")
        with self.assertRaisesRegex(ValueError, "ГГГГ-ММ-ДД"):
            self.store.create("Задача", due_date="31.12.2026")

    def test_update_status_and_delete(self) -> None:
        task = self.store.create("Черновик")
        updated = self.store.update(
            task.id, "  Итоговый вариант ", "Описание", "Учеба", "Низкий", "2026-12-31"
        )
        self.assertEqual(updated.title, "Итоговый вариант")
        self.assertEqual(updated.description, "Описание")
        self.assertEqual(updated.category, "Учеба")
        self.assertEqual(updated.due_date, "2026-12-31")

        completed = self.store.set_completed(task.id, True)
        self.assertTrue(completed.completed)
        self.assertEqual(len(self.store.list_tasks(completed=True)), 1)
        self.store.delete(task.id)
        self.assertEqual(self.store.list_tasks(), [])

    def test_search_category_priority_and_status_filters(self) -> None:
        self.store.create("Купить молоко", "У магазина", "Покупки", "Низкий")
        urgent = self.store.create("Подготовить проект", "К презентации", "Работа", "Высокий")
        self.store.create("Купить сыр", category="Покупки", priority="Средний")

        self.assertEqual(len(self.store.list_tasks(search="магазина")), 1)
        self.assertEqual(len(self.store.list_tasks(search="ПОКУП")), 2)
        self.assertEqual(len(self.store.list_tasks(category="Работа")), 1)
        self.assertEqual(len(self.store.list_tasks(priority="Высокий")), 1)
        by_priority = self.store.list_tasks(sort_by="priority")
        self.assertEqual(by_priority[0].id, urgent.id)
        self.store.set_completed(urgent.id, True)
        self.assertEqual(len(self.store.list_tasks(completed=False)), 2)

    def test_data_survives_reopening_database(self) -> None:
        task = self.store.create("Сохраненная задача", due_date="2026-11-10")
        self.store.close()
        self.store = TaskStore(self.database)

        restored = self.store.get(task.id)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.title, "Сохраненная задача")
        self.assertEqual(restored.due_date, "2026-11-10")


if __name__ == "__main__":
    unittest.main()
