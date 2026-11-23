"""The per-session ``unattended`` switch (design.md, "Unattended sessions").

On, a session refuses whatever would otherwise ask the user: the approval
hook denies every token, with a reason the agent can act on. Work that needs
no token -- reads, and what the profile allows inside the sandbox roots --
goes on. Off (the default), tokens ask as always.

It grants nothing in either state, so it is not kept with the site file and
needs no protection from the agent: on, outside work is refused; off, it
asks. The worst a wrong switch can do is refuse something, or leave a prompt
waiting.

State: one small JSON file per session under the audit directory,
``unattended/<session>.json``: ``{"since": ISO, "until": ISO or null}``.
A file past its ``until`` reads as off. The approval hook reads the same
file with its own few lines (hooks import nothing from here).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from hpctether import audit


def state_dir() -> Path:
    return audit.audit_dir() / "unattended"


def state_file(session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session) or "none"
    return state_dir() / f"{safe}.json"


def current(session: str, now: datetime | None = None) -> dict | None:
    """The session's state if the switch is on and not expired, else None."""
    if not session:
        return None
    try:
        data = json.loads(state_file(session).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("since"):
        return None  # switched off: the file holds {}
    until = data.get("until")
    if until:
        try:
            end = datetime.fromisoformat(until)
        except ValueError:
            return None
        if (now or datetime.now(timezone.utc)) >= end:
            return None
    return data


def switch_on(session: str, until: datetime | None) -> dict:
    data = {
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "until": until.astimezone(timezone.utc).isoformat(timespec="seconds") if until else None,
    }
    path = state_file(session)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)
    return data


def switch_off(session: str) -> bool:
    """Turn it off. True if it was on. The file is truncated to ``{}``, never
    deleted: the toolkit deletes nothing, even its own state."""
    was = current(session) is not None
    path = state_file(session)
    if path.exists():
        path.write_text("{}")
    return was


def parse_until(text: str, now: datetime | None = None) -> datetime:
    """``HH:MM`` local time, the next time that clock time comes round."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"until {text!r} is not a time like 08:00")
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    end = local_now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                            second=0, microsecond=0)
    if end <= local_now:
        from datetime import timedelta
        end += timedelta(days=1)
    return end


def describe(data: dict | None) -> str:
    """``UNATTENDED until 08:00`` / ``UNATTENDED`` / ``attended``."""
    if not data:
        return "attended"
    until = data.get("until")
    if not until:
        return "UNATTENDED"
    try:
        end = datetime.fromisoformat(until).astimezone()
    except ValueError:
        return "UNATTENDED"
    return "UNATTENDED until " + end.strftime("%H:%M")
