"""Перевод отчёта водителя в деньги по условиям проекта. Чистые функции, без БД."""
import math
from dataclasses import dataclass, fields
from datetime import datetime

EXPENSE_KINDS = {
    "toll": "Платная дорога",
    "parking": "Парковка",
    "fuel": "Топливо",
    "other": "Прочее",
}

RATE_FIELDS = {
    "shift_rate": "Ставка за смену, ₽",
    "shift_hours": "Длина смены, ч",
    "overtime_rate": "Переработка, ₽/ч",
    "overtime_step_min": "Шаг округления переработки, мин",
    "km_included": "Км, включённые в смену",
    "km_rate": "Сверх лимита, ₽/км",
}


@dataclass(frozen=True)
class Rates:
    shift_rate: float = 0
    shift_hours: float = 12
    overtime_rate: float = 0
    overtime_step_min: int = 60
    km_included: float = 0
    km_rate: float = 0

    @classmethod
    def merged(cls, project: dict, member: dict | None = None) -> "Rates":
        """Условия проекта, перекрытые персональными условиями водителя (если заданы)."""
        values = {}
        for f in fields(cls):
            v = member.get(f.name) if member else None
            values[f.name] = project[f.name] if v is None else v
        return cls(**values)


@dataclass(frozen=True)
class ShiftCalc:
    hours: float
    overtime_hours: float
    km: float
    km_over: float
    shift_pay: float
    overtime_pay: float
    km_pay: float
    expenses: dict[str, float]

    @property
    def expenses_total(self) -> float:
        return sum(self.expenses.values())

    @property
    def total(self) -> float:
        return self.shift_pay + self.overtime_pay + self.km_pay + self.expenses_total


def shift_hours(start: datetime | None, end: datetime | None) -> float:
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600)


def overtime_hours(hours: float, rates: Rates) -> float:
    over_min = (hours - rates.shift_hours) * 60
    # Защита от погрешности float: 12:00:00 → ровно 0 минут переработки
    over_min = round(over_min, 6)
    if over_min <= 0:
        return 0.0
    step = rates.overtime_step_min
    if step and step > 0:
        over_min = math.ceil(over_min / step) * step
    return over_min / 60


def calc_shift(
    start: datetime | None,
    end: datetime | None,
    km: float | None,
    expenses: list[tuple[str, float]],
    rates: Rates,
) -> ShiftCalc:
    hours = shift_hours(start, end)
    ot = overtime_hours(hours, rates) if end else 0.0
    km = km or 0.0
    km_over = max(0.0, km - rates.km_included) if rates.km_rate else 0.0
    exp = {k: 0.0 for k in EXPENSE_KINDS}
    for kind, amount in expenses:
        exp[kind if kind in exp else "other"] += amount
    return ShiftCalc(
        hours=hours,
        overtime_hours=ot,
        km=km,
        km_over=km_over,
        shift_pay=rates.shift_rate if end else 0.0,
        overtime_pay=ot * rates.overtime_rate,
        km_pay=km_over * rates.km_rate,
        expenses=exp,
    )


def money(v: float) -> str:
    """12345.5 → '12 345,50 ₽' (целые без копеек)."""
    if abs(v - round(v)) < 0.005:
        s = f"{round(v):,}".replace(",", " ")
    else:
        s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} ₽"


def num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",")
