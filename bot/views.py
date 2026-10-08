"""Сборка расчёта смены и текстов карточек."""
from datetime import date, datetime
from html import escape

from .calc import EXPENSE_KINDS, RATE_FIELDS, Rates, ShiftCalc, calc_shift, money, num
from .db import STATUS_LABELS, Database


def dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def hm(value: str | None) -> str:
    d = dt(value)
    return d.strftime("%H:%M") if d else "—"


def ru_date(value: str) -> str:
    return date.fromisoformat(value).strftime("%d.%m.%Y")


def shift_km(shift: dict) -> float | None:
    if shift.get("odo_start") is not None and shift.get("odo_end") is not None:
        return max(0.0, shift["odo_end"] - shift["odo_start"])
    return shift.get("km")


async def compute(db: Database, shift: dict, member: dict | None = None,
                  project: dict | None = None,
                  expenses: list[dict] | None = None) -> tuple[ShiftCalc, list[dict]]:
    member = member or await db.get_member(shift["member_id"])
    project = project or await db.get_project(member["project_id"])
    if expenses is None:
        expenses = await db.shift_expenses(shift["id"])
    calc = calc_shift(
        dt(shift["start_at"]),
        dt(shift["end_at"]),
        shift_km(shift),
        [(e["kind"], e["amount"]) for e in expenses],
        Rates.merged(project, member),
    )
    return calc, expenses


def shift_card(shift: dict, member: dict, calc: ShiftCalc, expenses: list[dict],
               with_driver: bool = False) -> str:
    lines = []
    head = f"<b>Смена {ru_date(shift['work_date'])}</b> · {STATUS_LABELS.get(shift['status'], shift['status'])}"
    lines.append(head)
    if with_driver:
        lines.append(f"👤 {escape(member['driver_name'])} · {escape(member['car'] or '—')}")
        lines.append(f"🏢 {escape(member['contractor'] or '—')}")
    lines.append(f"📁 {escape(member.get('project_name') or '')}")
    lines.append("")
    lines.append(f"🕘 {hm(shift['start_at'])} – {hm(shift['end_at'])}"
                 + (f" ({num(round(calc.hours, 2))} ч)" if shift["end_at"] else ""))
    if shift.get("odo_start") is not None:
        end = num(shift["odo_end"]) if shift.get("odo_end") is not None else "…"
        lines.append(f"🔢 Одометр: {num(shift['odo_start'])} → {end}")
    km = shift_km(shift)
    lines.append(f"🚗 Пробег: {num(km) + ' км' if km is not None else '—'}")
    if expenses:
        lines.append("")
        for e in expenses:
            mark = "🧾" if e["file_id"] else "⚠️ без чека"
            lines.append(f"• {EXPENSE_KINDS.get(e['kind'], e['kind'])}: {money(e['amount'])} {mark}")
    if shift["end_at"]:
        lines.append("")
        lines.append(f"Смена: {money(calc.shift_pay)}")
        if calc.overtime_hours:
            lines.append(f"Переработка {num(calc.overtime_hours)} ч: {money(calc.overtime_pay)}")
        if calc.km_pay:
            lines.append(f"Сверх лимита {num(calc.km_over)} км: {money(calc.km_pay)}")
        if calc.expenses_total:
            lines.append(f"Расходы по чекам: {money(calc.expenses_total)}")
        lines.append(f"<b>Итого: {money(calc.total)}</b>")
    if shift.get("admin_note"):
        lines.append("")
        lines.append(f"↩️ Возвращено: {escape(shift['admin_note'])}")
    return "\n".join(lines)


def rates_text(source: dict, inherited: dict | None = None) -> str:
    """Условия. Если передан inherited — показывает, что взято из проекта."""
    out = []
    for key, label in RATE_FIELDS.items():
        value = source.get(key)
        if value is None and inherited is not None:
            out.append(f"{label}: {num(inherited[key])} <i>(как в проекте)</i>")
        else:
            out.append(f"{label}: <b>{num(value)}</b>")
    return "\n".join(out)
