"""SQLite persistence, local accounts, and task synchronization records."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PRIORITIES = ("Низкий", "Средний", "Высокий")
SORT_OPTIONS = ("due_date", "created_at", "priority", "title")
PASSWORD_ITERATIONS = 310_000


@dataclass(frozen=True)
class User:
    id: int
    username: str


@dataclass(frozen=True)
class Task:
    id: int
    sync_id: str
    title: str
    description: str
    category: str
    priority: str
    due_date: str | None
    completed: bool
    created_at: str
    updated_at: str
    deleted_at: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_salt BLOB NOT NULL,
            password_hash BLOB NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Без категории',
            priority TEXT NOT NULL CHECK (priority IN ('Низкий', 'Средний', 'Высокий')),
            due_date TEXT,
            completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_preferences (
            user_id INTEGER PRIMARY KEY,
            path TEXT
        )
        """
    )
    existing_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
    }
    migrations = (
        ("user_id", "INTEGER"),
        ("sync_id", "TEXT"),
        ("updated_at", "TEXT"),
        ("deleted_at", "TEXT"),
    )
    for column, sql_type in migrations:
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} {sql_type}")

    rows = connection.execute(
        "SELECT id, created_at, sync_id, updated_at FROM tasks"
    ).fetchall()
    for row in rows:
        created_at = _normalize_timestamp(row["created_at"])
        connection.execute(
            "UPDATE tasks SET sync_id = ?, created_at = ?, updated_at = ? WHERE id = ?",
            (
                row["sync_id"] or str(uuid.uuid4()),
                _normalize_timestamp(row["updated_at"] or row["created_at"]),
                created_at,
                row["id"],
            ),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_sync_id ON tasks(sync_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id, deleted_at)"
    )
    connection.commit()


class AuthStore:
    """Local account database; passwords are stored as salted PBKDF2 hashes."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        _initialize_database(self._connection)

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
        )

    def register(self, username: str, password: str) -> User:
        clean_username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{3,32}", clean_username):
            raise ValueError(
                "Имя пользователя: 3–32 символа, латинские буквы, цифры, _ или -."
            )
        if len(password) < 8:
            raise ValueError("Пароль должен содержать не менее 8 символов.")
        salt = secrets.token_bytes(16)
        try:
            cursor = self._connection.execute(
                """
                INSERT INTO users (username, password_salt, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    clean_username,
                    salt,
                    self._password_hash(password, salt),
                    utc_now(),
                ),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            if "users.username" in str(error).lower() or "unique" in str(error).lower():
                raise ValueError("Это имя пользователя уже занято.") from error
            raise
        return User(cursor.lastrowid, clean_username)

    def authenticate(self, username: str, password: str) -> User:
        clean_username = username.strip().lower()
        row = self._connection.execute(
            "SELECT id, username, password_salt, password_hash FROM users WHERE username = ?",
            (clean_username,),
        ).fetchone()
        if row is None:
            self._password_hash(password, b"\0" * 16)
            raise ValueError("Неверное имя пользователя или пароль.")
        actual_hash = self._password_hash(password, row["password_salt"])
        if not hmac.compare_digest(actual_hash, row["password_hash"]):
            raise ValueError("Неверное имя пользователя или пароль.")
        return User(row["id"], row["username"])


class TaskStore:
    """SQLite-backed repository scoped to one authenticated local account."""

    def __init__(self, database_path: str | Path, user_id: int = 1) -> None:
        self.database_path = str(database_path)
        self.user_id = user_id
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function("casefold", 1, lambda value: value.casefold())
        _initialize_database(self._connection)

    def close(self) -> None:
        self._connection.close()

    def claim_legacy_tasks(self) -> int:
        cursor = self._connection.execute(
            "UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (self.user_id,)
        )
        self._connection.commit()
        return cursor.rowcount

    def sync_path(self) -> str | None:
        row = self._connection.execute(
            "SELECT path FROM sync_preferences WHERE user_id = ?", (self.user_id,)
        ).fetchone()
        return row["path"] if row else None

    def set_sync_path(self, path: str | None) -> None:
        self._connection.execute(
            """
            INSERT INTO sync_preferences (user_id, path) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET path = excluded.path
            """,
            (self.user_id, path),
        )
        self._connection.commit()

    @staticmethod
    def _validate(
        title: str,
        priority: str,
        due_date: str | None,
    ) -> tuple[str, str | None]:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("Введите название задачи.")
        if priority not in PRIORITIES:
            raise ValueError("Выберите допустимый приоритет.")
        clean_due_date = due_date.strip() if due_date else ""
        if clean_due_date:
            try:
                date.fromisoformat(clean_due_date)
            except ValueError as error:
                raise ValueError("Срок укажите в формате ГГГГ-ММ-ДД.") from error
        return clean_title, clean_due_date or None

    @staticmethod
    def _to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            sync_id=row["sync_id"],
            title=row["title"],
            description=row["description"],
            category=row["category"],
            priority=row["priority"],
            due_date=row["due_date"],
            completed=bool(row["completed"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    def create(
        self,
        title: str,
        description: str = "",
        category: str = "",
        priority: str = "Средний",
        due_date: str | None = None,
    ) -> Task:
        clean_title, clean_due_date = self._validate(title, priority, due_date)
        now = utc_now()
        cursor = self._connection.execute(
            """
            INSERT INTO tasks (
                user_id, sync_id, title, description, category, priority,
                due_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.user_id,
                str(uuid.uuid4()),
                clean_title,
                description.strip(),
                category.strip() or "Без категории",
                priority,
                clean_due_date,
                now,
                now,
            ),
        )
        self._connection.commit()
        task = self.get(cursor.lastrowid)
        assert task is not None
        return task

    def get(self, task_id: int) -> Task | None:
        row = self._connection.execute(
            """
            SELECT * FROM tasks
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (task_id, self.user_id),
        ).fetchone()
        return self._to_task(row) if row else None

    def update(
        self,
        task_id: int,
        title: str,
        description: str = "",
        category: str = "",
        priority: str = "Средний",
        due_date: str | None = None,
    ) -> Task:
        clean_title, clean_due_date = self._validate(title, priority, due_date)
        cursor = self._connection.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, category = ?, priority = ?, due_date = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (
                clean_title,
                description.strip(),
                category.strip() or "Без категории",
                priority,
                clean_due_date,
                utc_now(),
                task_id,
                self.user_id,
            ),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"Задача с номером {task_id} не найдена.")
        task = self.get(task_id)
        assert task is not None
        return task

    def delete(self, task_id: int) -> None:
        now = utc_now()
        cursor = self._connection.execute(
            """
            UPDATE tasks SET deleted_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (now, now, task_id, self.user_id),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"Задача с номером {task_id} не найдена.")

    def set_completed(self, task_id: int, completed: bool) -> Task:
        cursor = self._connection.execute(
            """
            UPDATE tasks SET completed = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (int(completed), utc_now(), task_id, self.user_id),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            raise KeyError(f"Задача с номером {task_id} не найдена.")
        task = self.get(task_id)
        assert task is not None
        return task

    def list_tasks(
        self,
        search: str = "",
        completed: bool | None = None,
        category: str | None = None,
        priority: str | None = None,
        sort_by: str = "due_date",
    ) -> list[Task]:
        if sort_by not in SORT_OPTIONS:
            raise ValueError(f"Сортировка должна быть одной из: {', '.join(SORT_OPTIONS)}.")

        conditions = ["user_id = ?", "deleted_at IS NULL"]
        parameters: list[Any] = [self.user_id]
        if search.strip():
            conditions.append(
                "instr(casefold(title || ' ' || description || ' ' || category), casefold(?)) > 0"
            )
            parameters.append(search.strip())
        if completed is not None:
            conditions.append("completed = ?")
            parameters.append(int(completed))
        if category:
            conditions.append("category = ?")
            parameters.append(category)
        if priority:
            conditions.append("priority = ?")
            parameters.append(priority)

        where = f"WHERE {' AND '.join(conditions)}"
        if sort_by == "priority":
            ordering = (
                "CASE priority WHEN 'Высокий' THEN 0 WHEN 'Средний' THEN 1 ELSE 2 END, "
                "due_date IS NULL, due_date, id DESC"
            )
        elif sort_by == "due_date":
            ordering = "due_date IS NULL, due_date, id DESC"
        elif sort_by == "created_at":
            ordering = "created_at DESC, id DESC"
        else:
            ordering = "title COLLATE NOCASE, id DESC"

        rows = self._connection.execute(
            f"SELECT * FROM tasks {where} ORDER BY {ordering}", parameters
        ).fetchall()
        return [self._to_task(row) for row in rows]

    def categories(self) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT category FROM tasks
            WHERE user_id = ? AND deleted_at IS NULL
            ORDER BY category COLLATE NOCASE
            """,
            (self.user_id,),
        ).fetchall()
        return [row["category"] for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sync_id": row["sync_id"],
            "title": row["title"],
            "description": row["description"],
            "category": row["category"],
            "priority": row["priority"],
            "due_date": row["due_date"],
            "completed": bool(row["completed"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }

    def export_sync_records(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY sync_id",
            (self.user_id,),
        ).fetchall()
        return [self._record(row) for row in rows]

    def merge_sync_records(self, records: list[dict[str, Any]]) -> int:
        records = [self._validate_sync_record(record) for record in records]
        changed = 0
        try:
            for clean in records:
                local_row = self._connection.execute(
                    "SELECT * FROM tasks WHERE sync_id = ? AND user_id = ?",
                    (clean["sync_id"], self.user_id),
                ).fetchone()
                if local_row:
                    local = self._record(local_row)
                    local_key = (local["updated_at"], repr(sorted(local.items())))
                    remote_key = (clean["updated_at"], repr(sorted(clean.items())))
                    if remote_key <= local_key:
                        continue
                    self._connection.execute(
                        """
                        UPDATE tasks SET title = ?, description = ?, category = ?, priority = ?,
                            due_date = ?, completed = ?, created_at = ?, updated_at = ?, deleted_at = ?
                        WHERE sync_id = ? AND user_id = ?
                        """,
                        (
                            clean["title"], clean["description"], clean["category"],
                            clean["priority"], clean["due_date"], int(clean["completed"]),
                            clean["created_at"], clean["updated_at"], clean["deleted_at"],
                            clean["sync_id"], self.user_id,
                        ),
                    )
                else:
                    self._connection.execute(
                        """
                        INSERT INTO tasks (
                            user_id, sync_id, title, description, category, priority, due_date,
                            completed, created_at, updated_at, deleted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.user_id, clean["sync_id"], clean["title"], clean["description"],
                            clean["category"], clean["priority"], clean["due_date"],
                            int(clean["completed"]), clean["created_at"], clean["updated_at"],
                            clean["deleted_at"],
                        ),
                    )
                changed += 1
        except Exception:
            self._connection.rollback()
            raise
        self._connection.commit()
        return changed

    @classmethod
    def _validate_sync_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        required = {
            "sync_id", "title", "description", "category", "priority", "due_date",
            "completed", "created_at", "updated_at", "deleted_at",
        }
        if not isinstance(record, dict) or not required.issubset(record):
            raise ValueError("Файл синхронизации содержит задачу неверного формата.")
        try:
            sync_id = str(uuid.UUID(record["sync_id"]))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("В файле синхронизации найден неверный идентификатор задачи.") from error

        title, due_date = cls._validate(
            str(record["title"]), str(record["priority"]),
            record["due_date"] if isinstance(record["due_date"], str) else None,
        )
        if record["due_date"] is not None and due_date is None:
            raise ValueError("В файле синхронизации неверно указан срок задачи.")
        if not isinstance(record["completed"], bool):
            raise ValueError("В файле синхронизации неверный статус задачи.")
        timestamps: dict[str, str | None] = {}
        for field in ("created_at", "updated_at", "deleted_at"):
            value = record[field]
            if value is None and field == "deleted_at":
                timestamps[field] = None
                continue
            if not isinstance(value, str):
                raise ValueError("В файле синхронизации неверная дата задачи.")
            try:
                normalized = _normalize_timestamp(value)
            except ValueError as error:
                raise ValueError("В файле синхронизации неверная дата задачи.") from error
            timestamps[field] = normalized
        return {
            "sync_id": sync_id,
            "title": title,
            "description": str(record["description"]),
            "category": str(record["category"]),
            "priority": record["priority"],
            "due_date": due_date,
            "completed": record["completed"],
            **timestamps,
        }
