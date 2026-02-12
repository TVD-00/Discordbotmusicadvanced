from __future__ import annotations

import re


_HMS_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)


def parse_time_to_ms(value: str) -> int:
    raw = value.strip()
    if not raw:
        raise ValueError("empty")

    if ":" in raw:
        parts = raw.split(":")
        if len(parts) == 2:
            mm, ss = parts
            h = 0
            m = int(mm)
            s = int(ss)
        elif len(parts) == 3:
            hh, mm, ss = parts
            h = int(hh)
            m = int(mm)
            s = int(ss)
        else:
            raise ValueError("bad format")

        if m < 0 or s < 0 or s >= 60:
            raise ValueError("bad time")
        if h < 0:
            raise ValueError("bad time")

        return ((h * 3600) + (m * 60) + s) * 1000

    if raw.isdigit():
        return int(raw) * 1000

    m = _HMS_RE.match(raw.lower())
    if not m:
        raise ValueError("bad format")

    hh = int(m.group(1) or 0)
    mm = int(m.group(2) or 0)
    ss = int(m.group(3) or 0)
    return ((hh * 3600) + (mm * 60) + ss) * 1000


def format_ms(ms: int) -> str:
    total = max(0, int(ms // 1000))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)

    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
