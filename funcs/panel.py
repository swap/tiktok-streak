import time
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from .streak import left

COLORS = {"active": "green", "waiting": "yellow", "lost": "red"}

def _up(started):
    n = max(0, int(time.time()) - int(started or time.time()))
    return f"{n // 3600:02d}:{(n % 3600) // 60:02d}:{n % 60:02d}"

def _due(row, now):
    if row.get("status") == "lost":
        before = int(row.get("restorable_before") or 0)
        return left(before, now) if before else "NOW"
    return left(row.get("send_before") or row.get("day_end"), now)

def _next(bot, row, now):
    acts = []
    st = row.get("status") or ""
    if st == "lost":
        if bot.streaks.ok_rest(row, now):
            acts.append("restore")
        elif row.get("restore_blocked"):
            acts.append("blocked")
        return " ".join(acts) or "idle"
    if bot.msg and bot.streaks.wants(row, now):
        acts.append("send")
    if bot.msg and bot.streaks.nudge(row, now):
        acts.append("nudge")
    return " ".join(acts) or "idle"

def view(bots, events, started):
    now = int(time.time())
    bot = bots[0] if bots else None
    user = (bot.auth.user if bot else {}) or {}
    head = Table.grid(expand=True)
    head.add_column(ratio=1)
    head.add_column(justify="right")
    head.add_row(f"[bold cyan]tt[/]  @{user.get('username') or '?'}", f"up {_up(started)}")
    table = Table(expand=True, show_lines=False, pad_edge=False)
    table.add_column("peer", min_width=20)
    table.add_column("days", justify="right", width=5)
    table.add_column("status", width=8)
    table.add_column("send", width=9)
    table.add_column("next", width=12)
    if bot:
        for row in bot.streaks.list():
            st = row.get("status") or "?"
            label = str(row.get("id") or "")
            if row.get("is_group"):
                label = f"group {label}"
            table.add_row(
                label,
                str(row.get("streak") or 0),
                Text(st, style=COLORS.get(st, "white")),
                _due(row, now),
                _next(bot, row, now),
            )
    feed = Table(expand=True, show_header=False, box=None, padding=(0, 1))
    feed.add_column("t", width=8, style="dim")
    feed.add_column("m")
    for e in (events or [])[-10:]:
        feed.add_row(e.get("t", ""), e.get("m", ""))
    if not events:
        feed.add_row("", "watching")
    return Group(
        Panel(head, border_style="cyan"),
        Panel(table, title="streaks", border_style="magenta"),
        Panel(feed, title="events", border_style="blue"),
    )
