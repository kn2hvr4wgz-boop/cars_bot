"""Хранилище на SQLite. Все времена — ISO-строки с часовым поясом."""
import secrets
from datetime import datetime
from pathlib import Path

import aiosqlite

from .calc import RATE_FIELDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL DEFAULT '',
    username TEXT,
    active_member_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    invite_code TEXT UNIQUE NOT NULL,
    owner_id INTEGER NOT NULL,
    shift_rate REAL NOT NULL DEFAULT 0,
    shift_hours REAL NOT NULL DEFAULT 12,
    overtime_rate REAL NOT NULL DEFAULT 0,
    overtime_step_min INTEGER NOT NULL DEFAULT 60,
    km_included REAL NOT NULL DEFAULT 0,
    km_rate REAL NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    user_id INTEGER NOT NULL REFERENCES users(tg_id),
    driver_name TEXT NOT NULL,
    car TEXT NOT NULL DEFAULT '',
    contractor TEXT NOT NULL DEFAULT '',
    shift_rate REAL,
    shift_hours REAL,
    overtime_rate REAL,
    overtime_step_min INTEGER,
    km_included REAL,
    km_rate REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, user_id)
);
CREATE TABLE IF NOT EXISTS shifts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL REFERENCES members(id),
    work_date TEXT NOT NULL,
    start_at TEXT,
    end_at TEXT,
    odo_start REAL,
    odo_end REAL,
    km REAL,
    status TEXT NOT NULL DEFAULT 'open',
    admin_note TEXT,
    submitted_at TEXT,
    reviewed_at TEXT,
    reviewed_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shifts_member ON shifts(member_id, status);
CREATE TABLE IF NOT EXISTS expenses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    amount REAL NOT NULL,
    file_id TEXT,
    file_kind TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_shift ON expenses(shift_id);
"""

# open — смена идёт; draft — закрыта, не отправлена (или возвращена);
# submitted — на проверке; approved — принята
STATUS_LABELS = {
    "open": "идёт",
    "draft": "черновик",
    "submitted": "на проверке",
    "approved": "принята",
}
CURRENT_STATUSES = ("open", "draft")

MEMBER_FIELDS = {"driver_name", "car", "contractor", *RATE_FIELDS}
PROJECT_FIELDS = {"name", *RATE_FIELDS}
SHIFT_FIELDS = {"work_date", "start_at", "end_at", "odo_start", "odo_end", "km"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, *args) -> dict | None:
        async with self.conn.execute(sql, args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, *args) -> list[dict]:
        async with self.conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _exec(self, sql: str, *args) -> int:
        cur = await self.conn.execute(sql, args)
        await self.conn.commit()
        return cur.lastrowid

    # ---------- users ----------
    async def upsert_user(self, tg_id: int, full_name: str, username: str | None) -> None:
        await self._exec(
            """INSERT INTO users(tg_id, full_name, username, created_at) VALUES (?,?,?,?)
               ON CONFLICT(tg_id) DO UPDATE SET full_name=excluded.full_name,
               username=excluded.username""",
            tg_id, full_name, username, _now(),
        )

    async def get_user(self, tg_id: int) -> dict | None:
        return await self._one("SELECT * FROM users WHERE tg_id=?", tg_id)

    async def set_active_member(self, tg_id: int, member_id: int) -> None:
        await self._exec("UPDATE users SET active_member_id=? WHERE tg_id=?", member_id, tg_id)

    # ---------- projects ----------
    async def create_project(self, owner_id: int, name: str, **rates) -> int:
        code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        cols = ["name", "invite_code", "owner_id", "created_at", *rates.keys()]
        vals = [name, code, owner_id, _now(), *rates.values()]
        return await self._exec(
            f"INSERT INTO projects({','.join(cols)}) VALUES ({','.join('?' * len(vals))})",
            *vals,
        )

    async def get_project(self, project_id: int) -> dict | None:
        return await self._one("SELECT * FROM projects WHERE id=?", project_id)

    async def get_project_by_code(self, code: str) -> dict | None:
        return await self._one(
            "SELECT * FROM projects WHERE invite_code=? AND archived=0", code
        )

    async def list_projects(self, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM projects"
        if not include_archived:
            sql += " WHERE archived=0"
        return await self._all(sql + " ORDER BY id DESC")

    async def update_project(self, project_id: int, field: str, value) -> None:
        if field not in PROJECT_FIELDS | {"archived"}:
            raise ValueError(field)
        await self._exec(f"UPDATE projects SET {field}=? WHERE id=?", value, project_id)

    # ---------- members ----------
    async def add_member(
        self, project_id: int, user_id: int, driver_name: str, car: str, contractor: str
    ) -> int:
        existing = await self._one(
            "SELECT id FROM members WHERE project_id=? AND user_id=?", project_id, user_id
        )
        if existing:
            await self._exec(
                "UPDATE members SET driver_name=?, car=?, contractor=?, active=1 WHERE id=?",
                driver_name, car, contractor, existing["id"],
            )
            return existing["id"]
        return await self._exec(
            """INSERT INTO members(project_id, user_id, driver_name, car, contractor, created_at)
               VALUES (?,?,?,?,?,?)""",
            project_id, user_id, driver_name, car, contractor, _now(),
        )

    async def get_member(self, member_id: int) -> dict | None:
        return await self._one(
            """SELECT m.*, p.name AS project_name FROM members m
               JOIN projects p ON p.id=m.project_id WHERE m.id=?""",
            member_id,
        )

    async def user_memberships(self, user_id: int) -> list[dict]:
        return await self._all(
            """SELECT m.*, p.name AS project_name FROM members m
               JOIN projects p ON p.id=m.project_id
               WHERE m.user_id=? AND m.active=1 AND p.archived=0 ORDER BY m.id DESC""",
            user_id,
        )

    async def project_members(self, project_id: int) -> list[dict]:
        return await self._all(
            """SELECT * FROM members WHERE project_id=? AND active=1
               ORDER BY contractor, driver_name""",
            project_id,
        )

    async def update_member(self, member_id: int, field: str, value) -> None:
        if field not in MEMBER_FIELDS | {"active"}:
            raise ValueError(field)
        await self._exec(f"UPDATE members SET {field}=? WHERE id=?", value, member_id)

    # ---------- shifts ----------
    async def current_shift(self, member_id: int) -> dict | None:
        return await self._one(
            f"""SELECT * FROM shifts WHERE member_id=? AND status IN
                ({','.join('?' * len(CURRENT_STATUSES))}) ORDER BY id DESC LIMIT 1""",
            member_id, *CURRENT_STATUSES,
        )

    async def create_shift(self, member_id: int, work_date: str, **values) -> int:
        bad = set(values) - SHIFT_FIELDS - {"status"}
        if bad:
            raise ValueError(bad)
        cols = ["member_id", "work_date", "created_at", *values.keys()]
        vals = [member_id, work_date, _now(), *values.values()]
        return await self._exec(
            f"INSERT INTO shifts({','.join(cols)}) VALUES ({','.join('?' * len(vals))})", *vals
        )

    async def get_shift(self, shift_id: int) -> dict | None:
        return await self._one("SELECT * FROM shifts WHERE id=?", shift_id)

    async def update_shift(self, shift_id: int, **values) -> None:
        bad = set(values) - SHIFT_FIELDS - {"status", "admin_note"}
        if bad:
            raise ValueError(bad)
        sets = ", ".join(f"{k}=?" for k in values)
        await self._exec(f"UPDATE shifts SET {sets} WHERE id=?", *values.values(), shift_id)

    async def submit_shift(self, shift_id: int) -> None:
        await self._exec(
            "UPDATE shifts SET status='submitted', submitted_at=?, admin_note=NULL WHERE id=?",
            _now(), shift_id,
        )

    async def review_shift(
        self, shift_id: int, admin_id: int, approve: bool, note: str | None = None
    ) -> None:
        await self._exec(
            "UPDATE shifts SET status=?, reviewed_at=?, reviewed_by=?, admin_note=? WHERE id=?",
            "approved" if approve else "draft", _now(), admin_id, note, shift_id,
        )

    async def delete_shift(self, shift_id: int) -> None:
        await self._exec("DELETE FROM shifts WHERE id=?", shift_id)

    async def member_shifts(self, member_id: int, limit: int = 10) -> list[dict]:
        return await self._all(
            "SELECT * FROM shifts WHERE member_id=? ORDER BY work_date DESC, id DESC LIMIT ?",
            member_id, limit,
        )

    async def pending_shifts(self) -> list[dict]:
        return await self._all(
            """SELECT s.*, m.driver_name, m.car, m.contractor, m.project_id, p.name AS project_name
               FROM shifts s JOIN members m ON m.id=s.member_id
               JOIN projects p ON p.id=m.project_id
               WHERE s.status='submitted' ORDER BY s.submitted_at"""
        )

    async def shifts_for_export(
        self,
        project_id: int,
        date_from: str | None,
        date_to: str | None,
        statuses: tuple[str, ...] = ("submitted", "approved"),
        contractor: str | None = None,
    ) -> list[dict]:
        sql = f"""SELECT s.*, m.driver_name, m.car, m.contractor, m.user_id
                  FROM shifts s JOIN members m ON m.id=s.member_id
                  WHERE m.project_id=? AND s.status IN ({','.join('?' * len(statuses))})"""
        args: list = [project_id, *statuses]
        if date_from:
            sql += " AND s.work_date>=?"
            args.append(date_from)
        if date_to:
            sql += " AND s.work_date<=?"
            args.append(date_to)
        if contractor is not None:
            sql += " AND m.contractor=?"
            args.append(contractor)
        sql += " ORDER BY m.contractor, s.work_date, m.driver_name, s.id"
        return await self._all(sql, *args)

    async def project_contractors(self, project_id: int) -> list[str]:
        rows = await self._all(
            "SELECT DISTINCT contractor FROM members WHERE project_id=? ORDER BY contractor",
            project_id,
        )
        return [r["contractor"] for r in rows]

    # ---------- expenses ----------
    async def add_expense(
        self, shift_id: int, kind: str, amount: float, file_id: str | None, file_kind: str | None
    ) -> int:
        return await self._exec(
            """INSERT INTO expenses(shift_id, kind, amount, file_id, file_kind, created_at)
               VALUES (?,?,?,?,?,?)""",
            shift_id, kind, amount, file_id, file_kind, _now(),
        )

    async def shift_expenses(self, shift_id: int) -> list[dict]:
        return await self._all("SELECT * FROM expenses WHERE shift_id=? ORDER BY id", shift_id)

    async def expenses_for_shifts(self, shift_ids: list[int]) -> dict[int, list[dict]]:
        result: dict[int, list[dict]] = {sid: [] for sid in shift_ids}
        if not shift_ids:
            return result
        rows = await self._all(
            f"SELECT * FROM expenses WHERE shift_id IN ({','.join('?' * len(shift_ids))}) ORDER BY id",
            *shift_ids,
        )
        for r in rows:
            result[r["shift_id"]].append(r)
        return result

    async def delete_expense(self, expense_id: int) -> None:
        await self._exec("DELETE FROM expenses WHERE id=?", expense_id)

    async def get_expense(self, expense_id: int) -> dict | None:
        return await self._one("SELECT * FROM expenses WHERE id=?", expense_id)
