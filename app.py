"""A polished Russian-language desktop task manager built with Tkinter."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from sync import SyncService
from tasks import PRIORITIES, SORT_OPTIONS, AuthStore, Task, TaskStore, User

APP_NAME = "Планер"
LEGACY_DATA_NAME = "TaskFlow"


def default_database_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    database = base / APP_NAME / "tasks.db"
    legacy_database = base / LEGACY_DATA_NAME / "tasks.db"
    if not database.exists() and legacy_database.exists():
        return legacy_database
    return database


class TaskDialog(tk.Toplevel):
    def __init__(
        self,
        parent: "TaskManagerApp",
        task: Task | None = None,
    ) -> None:
        super().__init__(parent)
        self.parent = parent
        self.task = task
        self.title("Редактировать задачу" if task else "Новая задача")
        self.configure(bg="#f3f5fa")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        frame = ttk.Frame(self, padding=24, style="Dialog.TFrame")
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(
            frame,
            text="Редактировать задачу" if task else "Новая задача",
            style="DialogTitle.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 18))

        self.title_var = tk.StringVar(value=task.title if task else "")
        self.category_var = tk.StringVar(value=task.category if task else "")
        self.priority_var = tk.StringVar(value=task.priority if task else "Средний")
        self.due_var = tk.StringVar(value=task.due_date if task and task.due_date else "")

        self._label(frame, "Название *", 1)
        title_entry = ttk.Entry(frame, textvariable=self.title_var, width=48)
        title_entry.grid(row=2, column=0, sticky="ew", pady=(5, 13))

        self._label(frame, "Описание", 3)
        self.description = tk.Text(
            frame,
            height=4,
            width=48,
            wrap="word",
            font=(parent.body_font, 10),
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
        )
        self.description.grid(row=4, column=0, sticky="ew", pady=(5, 13))
        if task:
            self.description.insert("1.0", task.description)

        options = ttk.Frame(frame, style="Dialog.TFrame")
        options.grid(row=5, column=0, sticky="ew")
        options.columnconfigure((0, 1), weight=1)

        left = ttk.Frame(options, style="Dialog.TFrame")
        left.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._label(left, "Категория", 0)
        ttk.Entry(left, textvariable=self.category_var).grid(
            row=1, column=0, sticky="ew", pady=(5, 13)
        )

        right = ttk.Frame(options, style="Dialog.TFrame")
        right.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._label(right, "Приоритет", 0)
        ttk.Combobox(
            right, textvariable=self.priority_var, values=PRIORITIES, state="readonly"
        ).grid(row=1, column=0, sticky="ew", pady=(5, 13))

        self._label(frame, "Срок (ГГГГ-ММ-ДД, необязательно)", 6)
        ttk.Entry(frame, textvariable=self.due_var).grid(
            row=7, column=0, sticky="ew", pady=(5, 18)
        )

        buttons = ttk.Frame(frame, style="Dialog.TFrame")
        buttons.grid(row=8, column=0, sticky="e")
        ttk.Button(buttons, text="Отмена", command=self.destroy).pack(side="right")
        ttk.Button(
            buttons, text="Сохранить", style="Accent.TButton", command=self._save
        ).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Control-Return>", lambda _event: self._save())
        title_entry.focus_set()
        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx() + 120}+{parent.winfo_rooty() + 90}")

    @staticmethod
    def _label(parent: ttk.Frame, text: str, row: int) -> None:
        ttk.Label(parent, text=text, style="Field.TLabel").grid(
            row=row, column=0, sticky="w"
        )

    def _save(self) -> None:
        try:
            values = (
                self.title_var.get(),
                self.description.get("1.0", "end-1c"),
                self.category_var.get(),
                self.priority_var.get(),
                self.due_var.get(),
            )
            if self.task:
                self.parent.store.update(self.task.id, *values)
            else:
                self.parent.store.create(*values)
        except ValueError as error:
            messagebox.showerror("Проверьте данные", str(error), parent=self)
            return
        self.parent.refresh()
        self.parent._sync_after_change()
        self.destroy()


class TaskManagerApp(tk.Tk):
    def __init__(self, database_path: str | Path | None = None) -> None:
        super().__init__()
        self.database_path = database_path or default_database_path()
        self.auth_store = AuthStore(self.database_path)
        self.store: TaskStore | None = None
        self.user: User | None = None
        self.title("Планер — задачи и планы")
        self.geometry("1100x740")
        self.minsize(850, 580)
        self.configure(bg="#f3f5fa")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._reminded_ids: set[int] = set()
        self._login_mode = "login"
        self._sync_job: str | None = None
        self._reminder_job: str | None = None
        families = set(tkfont.families(self))
        self.body_font = next(
            (name for name in ("Aptos", "Segoe UI", "Arial") if name in families),
            "TkDefaultFont",
        )
        self.display_font = next(
            (
                name
                for name in ("Aptos Display", "Segoe UI Variable Display", "Segoe UI")
                if name in families
            ),
            self.body_font,
        )

        self._setup_styles()
        self._show_login()

    def _setup_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=(self.body_font, 10))
        style.configure("App.TFrame", background="#f3f5fa")
        style.configure("Header.TFrame", background="#17243b")
        style.configure("BrandMark.TLabel", background="#273955", foreground="#8ce0c0",
                        font=(self.display_font, 15, "bold"), padding=(12, 9))
        style.configure("HeaderTitle.TLabel", background="#17243b", foreground="#ffffff",
                        font=(self.display_font, 24, "bold"))
        style.configure("HeaderSubtitle.TLabel", background="#17243b", foreground="#b6c2d4",
                        font=(self.body_font, 10))
        style.configure("HeaderCount.TLabel", background="#273955", foreground="#e2eaf4",
                        font=(self.body_font, 10, "bold"), padding=(14, 10))
        style.configure("LoginCard.TFrame", background="#ffffff")
        style.configure("LoginTitle.TLabel", background="#ffffff", foreground="#17243b",
                        font=(self.display_font, 22, "bold"))
        style.configure("LoginHint.TLabel", background="#ffffff", foreground="#7b879a",
                        font=(self.body_font, 10))
        style.configure("LoginError.TLabel", background="#ffffff", foreground="#b34d56",
                        font=(self.body_font, 9))
        style.configure("SyncStatus.TLabel", background="#17243b", foreground="#b6c2d4",
                        font=(self.body_font, 9))
        style.configure("Filters.TFrame", background="#ffffff")
        style.configure("FilterLabel.TLabel", background="#ffffff", foreground="#7b879a",
                        font=(self.body_font, 8, "bold"))
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("Dialog.TFrame", background="#f3f5fa")
        style.configure("Field.TLabel", background="#f3f5fa", foreground="#526078",
                        font=(self.body_font, 9, "bold"))
        style.configure("DialogTitle.TLabel", background="#f3f5fa", foreground="#17243b",
                        font=(self.display_font, 17, "bold"))
        style.configure("CardTitle.TLabel", background="#ffffff", foreground="#172033",
                        font=(self.display_font, 12, "bold"))
        style.configure("TButton", padding=(13, 9), background="#edf1f7",
                        foreground="#34425a", borderwidth=0, font=(self.body_font, 9, "bold"))
        style.map("TButton", background=[("active", "#dfe6f0")])
        style.configure("Accent.TButton", padding=(17, 10), background="#35a989",
                        foreground="#ffffff", borderwidth=0,
                        font=(self.body_font, 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#278c70")])
        style.configure("Danger.TButton", padding=(13, 9), background="#fff0f0",
                        foreground="#b34d56", borderwidth=0,
                        font=(self.body_font, 9, "bold"))
        style.map("Danger.TButton", background=[("active", "#ffe0e2")])
        style.configure("Treeview", rowheight=46, background="#ffffff",
                        fieldbackground="#ffffff", foreground="#34425a",
                        borderwidth=0, font=(self.body_font, 10))
        style.configure("Treeview.Heading", background="#f6f8fb",
                        foreground="#7b879a", relief="flat",
                        font=(self.body_font, 8, "bold"), padding=(10, 12))
        style.map("Treeview", background=[("selected", "#e5f5ef")],
                  foreground=[("selected", "#17243b")])
        style.configure("TCombobox", padding=8, fieldbackground="#ffffff",
                        background="#ffffff", foreground="#34425a")
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")],
                  foreground=[("readonly", "#34425a")])
        style.configure("TEntry", padding=9, fieldbackground="#ffffff",
                        foreground="#34425a")

    def _show_login(self) -> None:
        self.login_frame = ttk.Frame(self, style="App.TFrame")
        self.login_frame.pack(fill="both", expand=True)
        card = ttk.Frame(self.login_frame, padding=(38, 34), style="LoginCard.TFrame")
        card.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(card, text="П", style="BrandMark.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 22)
        )
        ttk.Label(card, text="Планер", style="LoginTitle.TLabel").grid(
            row=1, column=0, sticky="w"
        )
        ttk.Label(
            card, text="Войдите, чтобы открыть свои задачи", style="LoginHint.TLabel"
        ).grid(row=2, column=0, sticky="w", pady=(4, 22))
        ttk.Label(card, text="Имя пользователя").grid(
            row=3, column=0, sticky="w", pady=(0, 5)
        )
        self.username_var = tk.StringVar()
        username_entry = ttk.Entry(card, textvariable=self.username_var, width=38)
        username_entry.grid(row=4, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(card, text="Пароль").grid(row=5, column=0, sticky="w", pady=(0, 5))
        self.password_var = tk.StringVar()
        password_entry = ttk.Entry(card, textvariable=self.password_var, show="●", width=38)
        password_entry.grid(row=6, column=0, sticky="ew", pady=(0, 10))
        self.login_error = ttk.Label(card, text="", style="LoginError.TLabel")
        self.login_error.grid(row=7, column=0, sticky="w", pady=(0, 8))
        self.login_submit = ttk.Button(
            card, text="Войти", style="Accent.TButton", command=self._authenticate
        )
        self.login_submit.grid(row=8, column=0, sticky="ew")
        self.login_switch = ttk.Button(
            card, text="Создать новый аккаунт", command=self._toggle_login_mode
        )
        self.login_switch.grid(row=9, column=0, sticky="ew", pady=(10, 0))
        password_entry.bind("<Return>", lambda _event: self._authenticate())
        username_entry.bind("<Return>", lambda _event: password_entry.focus_set())
        username_entry.focus_set()

    def _toggle_login_mode(self) -> None:
        self._login_mode = "register" if self._login_mode == "login" else "login"
        if self._login_mode == "register":
            self.login_submit.configure(text="Создать аккаунт")
            self.login_switch.configure(text="Уже есть аккаунт? Войти")
            self.login_error.configure(text="Пароль должен быть не короче 8 символов.")
        else:
            self.login_submit.configure(text="Войти")
            self.login_switch.configure(text="Создать новый аккаунт")
            self.login_error.configure(text="")

    def _authenticate(self) -> None:
        try:
            if self._login_mode == "register":
                user = self.auth_store.register(
                    self.username_var.get(), self.password_var.get()
                )
            else:
                user = self.auth_store.authenticate(
                    self.username_var.get(), self.password_var.get()
                )
        except ValueError as error:
            self.login_error.configure(text=str(error))
            return

        self.user = user
        self.store = TaskStore(self.database_path, user.id)
        self.store.claim_legacy_tasks()
        self.login_frame.destroy()
        self._build_ui()
        self.refresh()
        self._reminder_job = self.after(1000, self._check_reminders)
        self.after(3000, self._auto_sync)
        if self.store.sync_path():
            self._sync_now(show_success=False)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=(28, 22, 28, 24), style="App.TFrame")
        self.main_root = root
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        header = ttk.Frame(root, padding=(22, 18), style="Header.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="П", style="BrandMark.TLabel").grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 16)
        )
        ttk.Label(header, text="Планер", style="HeaderTitle.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        ttk.Label(
            header, text="Планы на день — под контролем",
            style="HeaderSubtitle.TLabel"
        ).grid(row=1, column=1, sticky="nw", pady=(1, 0))
        self.count_label = ttk.Label(header, style="HeaderCount.TLabel")
        self.count_label.grid(row=0, column=2, rowspan=2, sticky="e", padx=(14, 0))
        self.account_label = ttk.Label(
            header, text=f"Профиль: {self.user.username}", style="HeaderSubtitle.TLabel"
        )
        self.account_label.grid(row=0, column=3, sticky="e", padx=(18, 0))
        ttk.Button(header, text="Выйти", command=self._logout).grid(
            row=1, column=3, sticky="e", padx=(18, 0), pady=(7, 0)
        )
        self.sync_status = ttk.Label(
            header, text="Синхронизация не настроена", style="SyncStatus.TLabel"
        )
        self.sync_status.grid(row=2, column=1, sticky="w", pady=(13, 0))
        sync_buttons = ttk.Frame(header, style="Header.TFrame")
        sync_buttons.grid(row=2, column=2, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(
            sync_buttons, text="Настроить файл", command=self._choose_sync_file
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            sync_buttons,
            text="Синхронизировать",
            command=lambda: self._sync_now(show_success=True),
        ).pack(side="left")

        toolbar = ttk.Frame(root, padding=(14, 12), style="Filters.TFrame")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        toolbar.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        search = ttk.Entry(toolbar, textvariable=self.search_var)
        search.grid(row=0, column=0, sticky="ew", padx=(0, 10), ipady=4)
        search.insert(0, "⌕  Поиск по задачам")
        search.configure(foreground="#9aa5b5")
        search.bind("<FocusIn>", self._clear_search_hint)
        search.bind("<FocusOut>", self._restore_search_hint)
        search.bind("<KeyRelease>", lambda _event: self.refresh())
        self.status_var = tk.StringVar(value="Все задачи")
        self.status_filter = ttk.Combobox(
            toolbar, textvariable=self.status_var,
            values=("Все задачи", "Активные", "Выполненные"),
            state="readonly", width=16
        )
        self.status_filter.grid(row=0, column=1, padx=(0, 8), ipady=2)
        self.status_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        self.category_var = tk.StringVar(value="Все категории")
        self.category_filter = ttk.Combobox(
            toolbar, textvariable=self.category_var, state="readonly", width=17
        )
        self.category_filter.grid(row=0, column=2, padx=(0, 8), ipady=2)
        self.category_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        self.priority_var = tk.StringVar(value="Любой приоритет")
        self.priority_filter = ttk.Combobox(
            toolbar,
            textvariable=self.priority_var,
            values=("Любой приоритет", *PRIORITIES),
            state="readonly",
            width=16,
        )
        self.priority_filter.grid(row=0, column=3, padx=(0, 8), ipady=2)
        self.priority_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        self.sort_var = tk.StringVar(value="Сначала срок")
        self.sort_filter = ttk.Combobox(
            toolbar, textvariable=self.sort_var,
            values=("Сначала срок", "Новые сначала", "По приоритету", "По названию"),
            state="readonly", width=17
        )
        self.sort_filter.grid(row=0, column=4, padx=(0, 0), ipady=2)
        self.sort_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh())

        card = ttk.Frame(root, padding=1, style="Card.TFrame")
        card.grid(row=3, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        ttk.Label(card, text="Ваш список", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w", padx=20, pady=(17, 13)
        )
        columns = ("title", "category", "priority", "due_date", "status")
        self.tree = ttk.Treeview(card, columns=columns, show="headings", selectmode="browse")
        headings = (
            ("title", "Задача", 390, "w"),
            ("category", "Категория", 150, "w"),
            ("priority", "Приоритет", 115, "center"),
            ("due_date", "Срок", 130, "center"),
            ("status", "Статус", 130, "center"),
        )
        for column, label, width, anchor in headings:
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, minwidth=80, anchor=anchor)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(12, 0), pady=(0, 14))
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 8), pady=(0, 14))
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.tag_configure("done", foreground="#8490a2")
        self.tree.tag_configure("overdue", foreground="#c23d52")
        self.tree.tag_configure("today", foreground="#a56812")
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected())
        self.tree.bind("<Return>", lambda _event: self._edit_selected())

        actions = ttk.Frame(root, style="App.TFrame")
        actions.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(
            actions, text="+  Новая задача", style="Accent.TButton",
            command=self._new_task
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(actions, text="Изменить", command=self._edit_selected).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Button(actions, text="Завершить / вернуть", command=self._toggle_selected).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Button(actions, text="Удалить", style="Danger.TButton",
                   command=self._delete_selected).grid(
            row=0, column=3, padx=(8, 0)
        )

    def _clear_search_hint(self, event: tk.Event) -> None:
        if self.search_var.get() == "⌕  Поиск по задачам":
            self.search_var.set("")
            event.widget.configure(foreground="#34425a")

    def _restore_search_hint(self, event: tk.Event) -> None:
        if not self.search_var.get().strip():
            self.search_var.set("⌕  Поиск по задачам")
            event.widget.configure(foreground="#9aa5b5")
            self.refresh()

    def refresh(self) -> None:
        current_category = self.category_var.get() if hasattr(self, "category_var") else "Все категории"
        categories = ["Все категории", *self.store.categories()]
        self.category_filter.configure(values=categories)
        if current_category not in categories:
            current_category = "Все категории"
            self.category_var.set(current_category)

        status = self.status_var.get()
        completed = {"Активные": False, "Выполненные": True}.get(status)
        priority = self.priority_var.get()
        if priority == "Любой приоритет":
            priority = None
        sort_key = {
            "Сначала срок": SORT_OPTIONS[0],
            "Новые сначала": SORT_OPTIONS[1],
            "По приоритету": SORT_OPTIONS[2],
            "По названию": SORT_OPTIONS[3],
        }.get(self.sort_var.get(), SORT_OPTIONS[0])
        tasks = self.store.list_tasks(
            search=(
                ""
                if self.search_var.get() == "⌕  Поиск по задачам"
                else self.search_var.get()
            ),
            completed=completed,
            category=None if current_category == "Все категории" else current_category,
            priority=priority,
            sort_by=sort_key,
        )

        self.tree.delete(*self.tree.get_children())
        today = date.today().isoformat()
        for task in tasks:
            if task.completed:
                tag = "done"
            elif task.due_date and task.due_date < today:
                tag = "overdue"
            elif task.due_date == today:
                tag = "today"
            else:
                tag = ""
            self.tree.insert(
                "", "end", iid=str(task.id),
                values=(
                    task.title, task.category, task.priority,
                    task.due_date or "—",
                    "Выполнена" if task.completed else "Активна",
                ),
                tags=(tag,) if tag else (),
            )

        all_tasks = self.store.list_tasks()
        done_count = sum(task.completed for task in all_tasks)
        self.count_label.configure(
            text=f"{len(all_tasks) - done_count} активных   ·   {done_count} выполнено"
        )

    def _selected_task(self) -> Task | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("Выберите задачу", "Сначала выберите задачу в списке.", parent=self)
            return None
        return self.store.get(int(selected[0]))

    def _new_task(self) -> None:
        TaskDialog(self)

    def _edit_selected(self) -> None:
        task = self._selected_task()
        if task:
            TaskDialog(self, task)

    def _toggle_selected(self) -> None:
        task = self._selected_task()
        if task:
            self.store.set_completed(task.id, not task.completed)
            self.refresh()
            self._sync_after_change()

    def _delete_selected(self) -> None:
        task = self._selected_task()
        if task and messagebox.askyesno(
            "Удалить задачу", f"Удалить «{task.title}»?", parent=self
        ):
            self.store.delete(task.id)
            self.refresh()
            self._sync_after_change()

    def _check_reminders(self) -> None:
        self._reminder_job = None
        if self.store is None:
            return
        today = date.today().isoformat()
        due_tasks = [
            task for task in self.store.list_tasks(completed=False)
            if task.due_date and task.due_date <= today and task.id not in self._reminded_ids
        ]
        if due_tasks:
            self._reminded_ids.update(task.id for task in due_tasks)
            lines = [
                f"• {task.title} — "
                f"{'срок сегодня' if task.due_date == today else 'просрочена'}"
                for task in due_tasks[:8]
            ]
            if len(due_tasks) > 8:
                lines.append(f"И еще задач: {len(due_tasks) - 8}")
            messagebox.showinfo("Напоминание о задачах", "\n".join(lines), parent=self)
        self._reminder_job = self.after(60_000, self._check_reminders)

    def _choose_sync_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Выберите файл синхронизации в общей облачной папке",
            initialfile="planer-sync.json",
            defaultextension=".json",
            filetypes=(("JSON", "*.json"),),
        )
        if not path:
            return
        self.store.set_sync_path(path)
        self._sync_now(show_success=True)

    def _sync_now(self, show_success: bool) -> None:
        if self.store is None or self.user is None:
            return
        path = self.store.sync_path()
        if not path:
            self.sync_status.configure(text="Выберите общий файл для синхронизации")
            if show_success:
                messagebox.showinfo(
                    "Синхронизация", "Сначала нажмите «Настроить файл».", parent=self
                )
            return
        try:
            imported = SyncService(path).synchronize(self.user.username, self.store)
        except ValueError as error:
            self.sync_status.configure(text=f"Ошибка синхронизации: {error}")
            if show_success:
                messagebox.showerror("Ошибка синхронизации", str(error), parent=self)
            return
        self.refresh()
        self.sync_status.configure(
            text=f"Синхронизировано · {datetime.now().strftime('%H:%M:%S')}"
        )
        if show_success:
            messagebox.showinfo(
                "Синхронизация завершена",
                f"Данные объединены. Обновлено задач: {imported}.",
                parent=self,
            )

    def _sync_after_change(self) -> None:
        if self.store and self.store.sync_path():
            self._sync_now(show_success=False)

    def _auto_sync(self) -> None:
        self._sync_job = None
        if self.store is None:
            return
        self._sync_after_change()
        self._sync_job = self.after(30_000, self._auto_sync)

    def _logout(self) -> None:
        if self._sync_job:
            self.after_cancel(self._sync_job)
            self._sync_job = None
        if self._reminder_job:
            self.after_cancel(self._reminder_job)
            self._reminder_job = None
        if self.store:
            self.store.close()
            self.store = None
        self.user = None
        self._reminded_ids.clear()
        self.main_root.destroy()
        self._login_mode = "login"
        self._show_login()

    def _close(self) -> None:
        if self._sync_job:
            self.after_cancel(self._sync_job)
        if self._reminder_job:
            self.after_cancel(self._reminder_job)
        if self.store:
            self.store.close()
        self.auth_store.close()
        self.destroy()


def main() -> None:
    app = TaskManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
