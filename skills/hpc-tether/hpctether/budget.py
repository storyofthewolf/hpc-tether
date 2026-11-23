"""Session transfer budget: a loop of small pulls cannot add up unseen.

The 2 GiB cap in hpc-pull and hpc-fetch bounds one call. It does not bound a
session: an agent once pulled 136 files one hpc-pull at a time, each
comfortably under the cap, and nothing would have stopped the same loop at
136 files of 1.9 GB. This module bounds the sum.

Every completed transfer is already an audit line carrying ``direction`` and
``bytes`` (hpctether/audit.py), so the running total is read from the log --
no second store to drift from it. Inbound bytes (cluster -> local) count
against the budget; outbound pushes are single files that already need
``--allow-write`` per write, and are reported but not budgeted.

The budget starts at 2 GiB per Claude Code session. A transfer that would take
the session past it is refused with exit 77, like any other operation that
needs the user's say-so, and nothing is sent. ``--budget-add SIZE`` raises the
budget by SIZE for the rest of the session; it is a confirmation token, passed
only after the user has agreed to that much more, and the grant is its own
audit line. One prompt per step of budget -- never one per file.

Outside a Claude Code session (no session id: the user running the verbs by
hand) there is no budget. The user typing the command is the approval.
"""

from __future__ import annotations

import json
import re

from hpctether import audit

DEFAULT_BUDGET = 2 * 1024**3

_UNITS = {"": 1, "B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)(?:I?B)?\s*$", re.IGNORECASE)


def parse_size(text: str) -> int:
    """``10G``, ``500M``, ``1.5GB``, ``2GiB`` or plain bytes. Raises ValueError."""
    m = _SIZE_RE.match(text or "")
    if not m:
        raise ValueError(f"not a size: {text!r} (use e.g. 500M, 10G)")
    n = int(float(m.group(1)) * _UNITS[m.group(2).upper()])
    if n <= 0:
        raise ValueError(f"size must be positive: {text!r}")
    return n


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _log_paths():
    """The current and previous month's logs: a session can straddle a month."""
    months = audit.existing_months()
    if not months:
        return [audit.audit_path()]
    return [audit.audit_path(m) for m in months[-2:]]


def session_entries(session: str) -> list[dict]:
    out = []
    for path in _log_paths():
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("session") == session:
                out.append(row)
    return out


def totals(rows: list[dict]) -> dict:
    """Bytes and files each way, and budget granted, over audit rows."""
    t = {"in_bytes": 0, "in_files": 0, "out_bytes": 0, "out_files": 0,
         "budget": DEFAULT_BUDGET}
    for r in rows:
        if r.get("kind") == "budget" and r.get("rc") == 0:
            t["budget"] += int(r.get("budget_add") or 0)
        d = r.get("direction")
        if d in ("in", "out") and r.get("bytes") is not None:
            t[f"{d}_bytes"] += int(r["bytes"])
            t[f"{d}_files"] += int(r.get("files") or 0)
    return t


def session_totals(session: str | None = None) -> dict | None:
    """Totals for ``session`` (default: the running one); None outside a session."""
    session = session if session is not None else audit._context()["session"]
    if not session:
        return None
    return totals(session_entries(session))


def status_line(t: dict | None) -> str:
    """One line for a verb's output: where this session stands."""
    if t is None:
        return "session: none (no transfer budget outside Claude Code)"
    out = f", {t['out_files']} files {human(t['out_bytes'])} out" if t["out_files"] else ""
    return (f"session: {t['in_files']} files {human(t['in_bytes'])} in"
            f"{out}; budget {human(t['budget'])}")


def admit(target, incoming: int, add: int, verb: str) -> str | None:
    """Check an inbound transfer of ``incoming`` bytes against the session budget.

    Returns a refusal message (the caller exits 77) or None to proceed. A
    refusal is logged as ``refused``. When ``add`` > 0 and the transfer then
    fits, the grant is logged as a ``budget`` line before anything is sent, so
    it stands for the rest of the session even if the transfer itself fails.
    """
    t = session_totals()
    if t is None:
        return None
    budget = t["budget"] + add
    after = t["in_bytes"] + incoming
    if after > budget:
        need = after - budget
        msg = (
            f"session transfer budget: this session has pulled "
            f"{human(t['in_bytes'])} ({t['in_files']} files) of a "
            f"{human(t['budget'])} budget"
            + (f" (+{human(add)} offered)" if add else "")
            + f"; this {verb} is {human(incoming)}, {human(need)} over.\n"
            "  Nothing was sent. Tell the user the running total and what this "
            "transfer is for; with their agreement, rerun with "
            f"--budget-add SIZE (at least {human(need)})."
        )
        audit.record("refused", target.name, target.host,
                     f"session budget: {human(after)} > {human(budget)}",
                     77)
        return msg
    if add:
        audit.record("budget", target.name, target.host,
                     f"budget +{human(add)} -> {human(budget)}", 0,
                     extra={"budget_add": add})
    return None
