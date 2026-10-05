"""When an instance polls: hours per weekday and weekend day, set by the person with /zeiten.

The poll timer fires every hour from FIRST_HOUR to LAST_HOUR; a timer poll outside the
person's hours ends right away, so no systemd changes are needed from the chat."""
from __future__ import annotations

import re
from datetime import datetime

FIRST_HOUR, LAST_HOUR = 7, 22  # must match OnCalendar in systemd/job-radar-poll@.timer
MAX_HOURS = 6
DEFAULT = {"weekdays": [9, 13, 18], "weekend": [9, 13, 18]}
PRESETS = {  # button data key: (label, schedule)
    "3x": ("3× am Tag", DEFAULT),
    "1x": ("1× am Tag", {"weekdays": [9], "weekend": [9]}),
    "workdays": ("Nur werktags, 3×", {"weekdays": [9, 13, 18], "weekend": []}),
    "hourly": ("Stündlich", {"weekdays": list(range(9, 22)), "weekend": list(range(9, 20, 2))}),
}


def due(plan: dict, now: datetime) -> bool:
    return now.hour in plan.get("weekend" if now.weekday() >= 5 else "weekdays", [])


def _hours(hours: list[int]) -> str:
    steps = {b - a for a, b in zip(hours, hours[1:])}
    if len(hours) >= 4 and len(steps) == 1:
        step = steps.pop()
        every = "stündlich" if step == 1 else f"alle {step} Stunden"
        return f"{every} von {hours[0]} bis {hours[-1]} Uhr"
    listed = ", ".join(map(str, hours[:-1])) + " und " if len(hours) > 1 else ""
    return f"um {listed}{hours[-1]} Uhr"


def describe(plan: dict) -> str:
    days, end = plan.get("weekdays", []), plan.get("weekend", [])
    if days == end:
        return f"jeden Tag {_hours(days)}" if days else "gar nicht"
    week = f"Mo–Fr {_hours(days)}" if days else "werktags nicht"
    return f"{week}, " + (f"Sa/So {_hours(end)}" if end else "am Wochenende nicht")


def parse(text: str) -> dict:
    """Hours like "8 12 17" for every day; raises ValueError with a hint for the person."""
    words = re.findall(r"\S+", text.replace(",", " "))
    hours = sorted({int(w) for w in words if w.isdigit()})
    rest = [w for w in words if not w.isdigit() and w.lower() not in ("uhr", "und")]
    if not hours or rest or not all(FIRST_HOUR <= h <= LAST_HOUR for h in hours) or len(hours) > MAX_HOURS:
        raise ValueError(f"Schick bis zu {MAX_HOURS} volle Stunden zwischen {FIRST_HOUR} und {LAST_HOUR}, "
                         "z. B. /zeiten 8 12 17")
    return {"weekdays": hours, "weekend": hours}
