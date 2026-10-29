import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DOCKER = "-docker" in sys.argv

from funcs.auth import env, load_auth
from funcs.msg import Inbox
from funcs.streak import Streaks, as_code, gap

ACTION_COOLDOWN_SECONDS = 3000
FAIL_BACKOFF_BASE_SECONDS = 30
FAIL_BACKOFF_MAX_SECONDS = 600
REFRESH_INTERVAL_SECONDS = 30
UI_UPDATE_INTERVAL_SECONDS = 0.25
REFRESH_BACKOFF_BASE_SECONDS = 5
REFRESH_BACKOFF_MAX_SECONDS = 300
BLOCK = (200008, 200009, 100024)
RESTORE = "restore"
SEND = "send"
NUDGE = "nudge"
AUTH_CODES = (-3,)

class Bot:
    def __init__(self, auth):
        self.auth = auth
        self.streaks = Streaks(auth)
        self.inbox = Inbox(auth)
        self.msg = env("STREAK_MESSAGE")
        self.gap = gap()
        self.ok_at = {}
        self.fail = {}
        self.fresh = False
        self.auth_dead = False
        self.last_ok = 0.0

    def ready(self):
        if not self.auth.ok_sess():
            return "no session"
        if not self.auth.login():
            return "bad session"
        return ""

    def refresh(self):
        if self.auth_dead:
            raise RuntimeError("authentication failed")
        rows = self.streaks.fetch()
        self.fresh = True
        self.last_ok = time.monotonic()
        return rows

    def cooled(self, key, wait=None):
        now = time.monotonic()
        limit = ACTION_COOLDOWN_SECONDS if wait is None else wait
        if now - float(self.ok_at.get(key) or 0) < limit:
            return True
        info = self.fail.get(key)
        if info and now < float(info.get("until") or 0):
            return True
        return False

    def mark_ok(self, key):
        self.ok_at[key] = time.monotonic()
        self.fail.pop(key, None)

    def mark_fail(self, key):
        now = time.monotonic()
        n = int((self.fail.get(key) or {}).get("n") or 0) + 1
        delay = min(FAIL_BACKOFF_MAX_SECONDS, FAIL_BACKOFF_BASE_SECONDS * (2 ** min(n - 1, 6)))
        self.fail[key] = {"n": n, "until": now + delay}

    def _auth_bad(self, out):
        code = as_code(out.get("code") if isinstance(out, dict) else None, -1)
        msg = str((out or {}).get("msg") or "").lower()
        if code in AUTH_CODES:
            return True
        if "login" in msg or "expired" in msg or "session" in msg:
            return True
        return False

    def _kill_auth(self, note, where):
        self.auth_dead = True
        note(f"authentication failed during {where}")

    def _restore(self, note, uid, cur):
        key = f"{uid}:{RESTORE}"
        if cur.get("status") != "lost" or not self.streaks.ok_rest(cur):
            return False
        out = self.streaks.restore(uid)
        if not isinstance(out, dict):
            note(f"restore failed for {uid}: invalid response")
            self.mark_fail(key)
            return True
        if self._auth_bad(out):
            self._kill_auth(note, "restore")
            return True
        ok = bool(out.get("ok"))
        code = as_code(out.get("code"), -1)
        if ok:
            self.mark_ok(key)
            note(f"restore {uid} ok")
            return True
        if code in BLOCK:
            self.streaks.block(uid, code)
        self.mark_fail(key)
        note(f"restore failed for {uid}: {out.get('msg') or code}")
        return True

    def _message(self, note, uid, cur, act):
        key = f"{uid}:{act}"
        if act == SEND and not self.streaks.wants(cur):
            return False
        if act == NUDGE and not self.streaks.nudge(cur):
            return False
        text = self.msg if act == SEND else f"{self.msg}?"
        out = self.inbox.send(uid, text, cur.get("conversation_id") or "", cur.get("conv_type") or 1)
        if not isinstance(out, dict):
            note(f"{act} failed for {uid}: invalid response")
            self.mark_fail(key)
            return True
        if self._auth_bad(out):
            self._kill_auth(note, act)
            return True
        if out.get("skip"):
            self.mark_fail(key)
            note(f"{act} skipped for {uid}: {out.get('msg') or 'blocked'}")
            return True
        ok = bool(out.get("ok"))
        if ok:
            self.mark_ok(key)
            self.streaks.touch(uid)
            note(f"{act} {uid} ok")
            return True
        self.mark_fail(key)
        note(f"{act} failed for {uid}: {out.get('msg') or as_code(out.get('code'), -1)}")
        return True

    def tick(self, note):
        if self.auth_dead:
            return False
        if not self.fresh:
            note("skipping actions: streak state is stale")
            return False
        changed = False
        for item in self.streaks.due():
            row = item["row"]
            uid = str(row.get("id") or "")
            for act in item["acts"]:
                key = f"{uid}:{act}"
                wait = self.gap if act == NUDGE else None
                if self.cooled(key, wait):
                    continue
                cur = self.streaks.rows.get(uid) or row
                if act == RESTORE:
                    if self._restore(note, uid, cur):
                        changed = True
                    if self.auth_dead:
                        return changed
                    continue
                if act not in (SEND, NUDGE) or not self.msg:
                    continue
                if self._message(note, uid, cur, act):
                    changed = True
                if self.auth_dead:
                    return changed
        if changed:
            try:
                self.refresh()
            except Exception as exc:
                self.fresh = False
                note(f"refresh error: {exc}")
        return changed

def _stamp():
    return datetime.now(tz=timezone.utc).strftime("%H:%M:%S")

def _log(msg):
    print(f"[{_stamp()}] {msg}", flush=True)

def _summary(bot):
    rows = bot.streaks.list()
    counts = {"active": 0, "waiting": 0, "lost": 0}
    for row in rows:
        st = row.get("status") or "?"
        counts[st] = counts.get(st, 0) + 1
    who = bot.auth.user.get("username") or bot.auth.user.get("id") or "?"
    return f"@{who} streaks={len(rows)} active={counts.get('active', 0)} waiting={counts.get('waiting', 0)} lost={counts.get('lost', 0)}"

def _loop_docker(bot, note):
    refresh_fails = 0
    next_refresh = time.monotonic()
    note(_summary(bot))
    while True:
        now = time.monotonic()
        if bot.auth_dead:
            note("authentication failed")
            time.sleep(REFRESH_INTERVAL_SECONDS)
            continue
        if now >= next_refresh:
            try:
                bot.refresh()
                bot.tick(note)
                refresh_fails = 0
                next_refresh = now + REFRESH_INTERVAL_SECONDS
                note(_summary(bot))
            except Exception as exc:
                bot.fresh = False
                refresh_fails += 1
                delay = min(
                    REFRESH_BACKOFF_MAX_SECONDS,
                    REFRESH_BACKOFF_BASE_SECONDS * (2 ** min(refresh_fails - 1, 6)),
                )
                next_refresh = now + delay
                msg = str(exc).lower()
                if "login" in msg or "auth" in msg or "session" in msg:
                    bot.auth_dead = True
                    note("authentication failed")
                else:
                    note(f"refresh failed: {exc}")
        time.sleep(1)

def _loop_rich(bot, note, events, started, bots):
    from rich.live import Live
    from funcs.panel import view

    with Live(view(bots, events, started), refresh_per_second=4, screen=True) as live:
        refresh_fails = 0
        next_refresh = time.monotonic()
        while True:
            now = time.monotonic()
            if bot.auth_dead:
                note("authentication failed")
                live.update(view(bots, events, started))
                time.sleep(UI_UPDATE_INTERVAL_SECONDS)
                continue
            if now >= next_refresh:
                try:
                    bot.refresh()
                    bot.tick(note)
                    refresh_fails = 0
                    next_refresh = now + REFRESH_INTERVAL_SECONDS
                except Exception as exc:
                    bot.fresh = False
                    refresh_fails += 1
                    delay = min(
                        REFRESH_BACKOFF_MAX_SECONDS,
                        REFRESH_BACKOFF_BASE_SECONDS * (2 ** min(refresh_fails - 1, 6)),
                    )
                    next_refresh = now + delay
                    msg = str(exc).lower()
                    if "login" in msg or "auth" in msg or "session" in msg:
                        bot.auth_dead = True
                        note("authentication failed")
                    else:
                        note(f"refresh failed: {exc}")
            live.update(view(bots, events, started))
            time.sleep(UI_UPDATE_INTERVAL_SECONDS)

def launch(docker=False):
    auth = load_auth(ROOT)
    bot = Bot(auth)
    why = bot.ready()
    if why == "no session":
        print("login failed: no TIKTOK_SESSIONID / TIKTOK_SID_TT in env, .env, or cookies.json")
        sys.exit(1)
    if why == "bad session":
        print("login failed: cookies present but TikTok rejected the session (expired or invalid)")
        sys.exit(1)
    try:
        bot.refresh()
    except Exception as exc:
        print(f"refresh failed: {exc}")
        sys.exit(1)
    who = bot.auth.user.get("username") or bot.auth.user.get("id") or "?"
    try:
        if docker:
            note = _log
            note(f"started docker mode as @{who}")
            _loop_docker(bot, note)
        else:
            events = []
            started = int(time.time())
            bots = [bot]
            def note(msg):
                events.append({"t": _stamp(), "m": msg})
                del events[:-10]
            _loop_rich(bot, note, events, started, bots)
    finally:
        auth.close()

if __name__ == "__main__":
    launch(docker=DOCKER)
