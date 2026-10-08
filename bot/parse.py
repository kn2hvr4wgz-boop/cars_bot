"""Разбор того, что водитель пишет руками: время, дата, суммы, километры."""
import re
from datetime import date, datetime, time, timedelta


def parse_number(text: str) -> float | None:
    """'1 250,50' / '1250.5' / '1250р' → 1250.5"""
    cleaned = re.sub(r"[^\d,.\-]", "", text.replace(" ", "")).replace(",", ".")
    if cleaned.count(".") > 1 or not cleaned or cleaned in {".", "-"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_time(text: str) -> time | None:
    """'8', '8:30', '08.30', '0830', '8 30' → time"""
    t = text.strip().lower().replace("ч", "")
    m = re.fullmatch(r"(\d{1,2})(?:[:.\s-]?(\d{2}))?", t)
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2) or 0)
    if h == 24 and mnt == 0:
        h = 0
    if h > 23 or mnt > 59:
        return None
    return time(h, mnt)


def parse_date(text: str, today: date) -> date | None:
    """'сегодня', 'вчера', '05.10', '5.10.2026', '2026-10-05' → date"""
    t = text.strip().lower()
    if t in {"сегодня", "today"}:
        return today
    if t in {"вчера", "yesterday"}:
        return today - timedelta(days=1)
    if t == "позавчера":
        return today - timedelta(days=2)
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", t)
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y = int(m.group(3)) if m.group(3) else today.year
    if y < 100:
        y += 2000
    try:
        result = date(y, mo, d)
    except ValueError:
        return None
    # "28.12" в январе — это прошлый год
    if not m.group(3) and result > today + timedelta(days=31):
        result = result.replace(year=y - 1)
    return result


def combine(work_date: date, t: time, tz, after: datetime | None = None) -> datetime:
    """Дата смены + время. Если конец раньше начала — значит смена ушла за полночь."""
    dt = datetime.combine(work_date, t, tzinfo=tz)
    if after is not None:
        while dt <= after:
            dt += timedelta(days=1)
    return dt
