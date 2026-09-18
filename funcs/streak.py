import time
from .auth import Auth, as_int

HOST = "https://api.tiktokv.com"
GET = f"{HOST}/tiktok/v1/im/streaks/get"
RESTORE_URL = f"{HOST}/tiktok/v1/im/streaks/restore"
ERRS = {
    0: "ok",
    5: "bad params",
    200003: "not mutual",
    200008: "unavailable",
    200009: "fail",
    100013: "fail",
    100019: "fail",
    100024: "blocked",
}
BAD = (200008, 200009, 100013, 100019, 100024)
RESTORE = "restore"
SEND = "send"
NUDGE = "nudge"

def as_code(val, default=-1):
    try:
        if val is None:
            return default
        return int(val)
    except (TypeError, ValueError):
        return default

def _var(buf, i):
    n = s = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << s
        if not b & 0x80:
            return n, i
        s += 7
        if s > 70:
            break
    return None, i

def _pb(buf):
    out = {}
    i = 0
    while i < len(buf):
        key, j = _var(buf, i)
        if key is None:
            break
        f, w = key >> 3, key & 7
        i = j
        if w == 0:
            v, i = _var(buf, i)
            if v is None:
                break
            out.setdefault(f, []).append(v)
        elif w == 2:
            n, i = _var(buf, i)
            if n is None or i + n > len(buf):
                break
            out.setdefault(f, []).append(buf[i : i + n])
            i += n
        elif w == 1:
            i += 8
        elif w == 5:
            i += 4
        else:
            break
    return out

def _s(vals, i=0):
    if not vals or i >= len(vals):
        return ""
    v = vals[i]
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode()
        except Exception:
            return ""
    return str(v)

def _n(vals, i=0, d=0):
    if not vals or i >= len(vals):
        return d
    v = vals[i]
    return v if isinstance(v, int) else d

def _zig(n):
    return n - (1 << 64) if n > 0x7FFFFFFFFFFFFFFF else n

def left(ts, now=None):
    now = int(now or time.time())
    secs = int(ts or 0) - now
    if secs <= 0:
        return "NOW"
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h >= 24:
        return f"{h // 24}d {h % 24}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"

def _pad():
    secs = as_int("WARN_SECS")
    if secs is not None:
        return max(0, secs)
    hrs = as_int("WARN_HOURS")
    return max(0, hrs * 3600) if hrs is not None else 0

def _row(chunk, my="", now=None):
    now = int(now or time.time())
    m = _pb(chunk)
    users = []
    for raw in m.get(1) or []:
        if not isinstance(raw, (bytes, bytearray)):
            continue
        u = _pb(raw)
        users.append({"uid": str(_n(u.get(1))), "ts": _n(u.get(2))})
    streak = _n(m.get(3))
    before = _n(m.get(5))
    restores = _n(m.get(6))
    level = _n(m.get(7))
    day_end = _n(m.get(11))
    day_next = _n(m.get(12))
    send_before = _n(m.get(15))
    ctype = _n(m.get(100))
    cid = _s(m.get(101))
    peer = ""
    my_ts = peer_ts = 0
    stamps = []
    for u in users:
        uid, ts = u["uid"], int(u["ts"] or 0)
        stamps.append(ts)
        if uid == my:
            my_ts = ts
        elif uid:
            peer, peer_ts = uid, ts
    if not peer and cid.startswith("0:1:"):
        parts = cid.split(":")
        if len(parts) >= 4:
            a, b = parts[2], parts[3]
            peer = b if a == my else a
    last = max(stamps) if stamps else 0
    missed = bool(day_end and now >= day_end and last < day_end)
    if missed and send_before and now >= send_before:
        status, style, flame = "lost", "red", False
    elif missed or (day_end and my_ts < day_end and now >= day_end):
        status, style, flame = "waiting", "yellow", False
    elif day_end and peer_ts and peer_ts < day_end and my_ts >= day_end and now >= day_end:
        status, style, flame = "waiting", "yellow", False
    else:
        status, style, flame = "active", "orange", True
    is_group = ctype == 2
    key = cid if is_group and cid else peer
    return {
        "id": key,
        "peer": peer,
        "is_group": is_group,
        "streak": streak,
        "level": level,
        "status": status,
        "style": style,
        "flame": flame,
        "restores": restores,
        "restorable_before": before,
        "send_before": send_before,
        "day_end": day_end,
        "day_next": day_next,
        "timezone": _s(m.get(4)),
        "tz_offset": _zig(_n(m.get(9))),
        "conversation_id": cid,
        "conv_type": ctype,
        "my_ts": my_ts,
        "peer_ts": peer_ts,
        "last_ts": last,
        "users": users,
        "checked_at": now,
    }

class Streaks:
    def __init__(self, auth=None):
        self.auth = auth or Auth()
        self.rows = {}
        self.blocked = {}

    def fetch(self, groups=True):
        if not self.auth.user.get("id") and not self.auth.login():
            raise RuntimeError("authentication failed")
        my = str(self.auth.user.get("id") or "")
        try:
            r = self.auth.client.get(
                GET,
                params=self.auth.params(),
                headers=self.auth.headers(
                    {"Accept": "application/x-protobuf,application/json", "Referer": "https://www.tiktok.com/messages"}
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"network error: {exc}") from exc
        if r.status_code in (401, 403):
            raise RuntimeError("authentication failed")
        if r.status_code != 200:
            raise RuntimeError(f"streaks/get {r.status_code}")
        if "json" in (r.headers.get("content-type") or "").lower():
            try:
                data = r.json()
            except Exception as exc:
                raise RuntimeError(f"API response invalid: {exc}") from exc
            raise RuntimeError(data.get("status_msg") or "API response invalid")
        if not r.content:
            raise RuntimeError("API response invalid: empty body")
        best = {}
        for chunk in _pb(r.content).get(1) or []:
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            row = _row(chunk, my=my)
            uid = str(row.get("id") or "")
            if not uid or int(row.get("streak") or 0) <= 0:
                continue
            if int(row.get("conv_type") or 0) == 2 and not groups:
                continue
            cur = best.get(uid)
            if not cur or int(row.get("streak") or 0) >= int(cur.get("streak") or 0):
                best[uid] = row
        for uid, row in best.items():
            blocked = uid in self.blocked and row["status"] == "lost"
            row["restore_blocked"] = blocked
            if uid in self.blocked and row["status"] != "lost":
                self.blocked.pop(uid, None)
        self.rows = best
        return sorted(best.values(), key=lambda x: int(x.get("streak") or 0), reverse=True)

    def list(self):
        return sorted(self.rows.values(), key=lambda x: int(x.get("streak") or 0), reverse=True)

    def dead(self):
        return [r for r in self.list() if r.get("status") == "lost"]

    def block(self, uid, code=0):
        uid = str(uid)
        self.blocked[uid] = {"at": int(time.time()), "code": as_code(code, 0)}
        if uid in self.rows:
            self.rows[uid]["restore_blocked"] = True

    def touch(self, uid, ts=None):
        uid = str(uid)
        row = self.rows.get(uid)
        if not row:
            return
        ts = int(ts or time.time())
        row["my_ts"] = ts
        day_end = int(row.get("day_end") or 0)
        peer_ts = int(row.get("peer_ts") or 0)
        if day_end and peer_ts >= day_end:
            row.update(status="active", style="orange", flame=True)
        elif row.get("status") != "lost":
            row.update(status="waiting", style="yellow", flame=False)

    def ok_rest(self, row, now=None):
        now = int(now or time.time())
        if not row or row.get("status") != "lost" or row.get("is_group"):
            return False
        uid = str(row.get("id") or "")
        if uid in self.blocked or row.get("restore_blocked"):
            return False
        if int(row.get("restores") or 0) <= 0:
            return False
        before = int(row.get("restorable_before") or 0)
        return not before or before > now

    def restore(self, uid):
        uid = str(uid)
        row = self.rows.get(uid) or {}
        if row.get("status") != "lost":
            return {"ok": False, "code": -5, "msg": "not lost", "id": uid}
        try:
            peer = int(uid)
        except (TypeError, ValueError):
            return {"ok": False, "code": -6, "msg": "API response invalid: bad uid", "id": uid}
        try:
            r = self.auth.client.post(
                RESTORE_URL,
                params=self.auth.params(),
                json={"peer_user_id": peer},
                headers=self.auth.headers(
                    {"Content-Type": "application/json", "Referer": "https://www.tiktok.com/messages"}
                ),
            )
        except Exception as e:
            return {"ok": False, "code": -1, "msg": f"network error: {e}", "id": uid}
        if r.status_code in (401, 403):
            return {"ok": False, "code": -3, "msg": "login", "id": uid}
        try:
            data = r.json() if r.content else {}
        except Exception:
            return {"ok": False, "code": -7, "msg": "API response invalid", "id": uid}
        if not isinstance(data, dict):
            return {"ok": False, "code": -7, "msg": "API response invalid", "id": uid}
        code = as_code(data.get("status_code"), -1)
        ok = code == 0
        if ok and uid in self.rows:
            self.rows[uid].update(status="active", style="orange", flame=True, my_ts=int(time.time()))
            self.blocked.pop(uid, None)
        elif code in BAD:
            self.block(uid, code)
        return {
            "ok": ok,
            "code": code,
            "msg": data.get("status_msg") or ERRS.get(code) or f"code {code}",
            "id": uid,
        }

    def wants(self, row, now=None):
        now = int(now or time.time())
        if not row or row.get("status") == "lost":
            return False
        my_ts = int(row.get("my_ts") or 0)
        day_end = int(row.get("day_end") or 0)
        send_before = int(row.get("send_before") or 0)
        if day_end and my_ts >= day_end:
            return False
        if row.get("status") == "waiting":
            return True
        pad = _pad()
        if send_before and now >= send_before - pad:
            return True
        return bool(day_end and now >= day_end - pad and my_ts < day_end)

    def nudge(self, row, now=None):
        now = int(now or time.time())
        if not row or row.get("status") == "lost" or row.get("is_group"):
            return False
        my_ts = int(row.get("my_ts") or 0)
        peer_ts = int(row.get("peer_ts") or 0)
        day_end = int(row.get("day_end") or 0)
        send_before = int(row.get("send_before") or day_end or 0)
        if not my_ts:
            return False
        if row.get("status") == "waiting" and day_end and my_ts >= day_end and peer_ts < day_end:
            return True
        if not send_before:
            return False
        return peer_ts < my_ts and now >= send_before - _pad()

    def due(self, now=None):
        now = int(now or time.time())
        out = []
        for row in self.list():
            acts = []
            if self.wants(row, now):
                acts.append(SEND)
            if row.get("status") == "lost" and self.ok_rest(row, now):
                acts.append(RESTORE)
            if self.nudge(row, now):
                acts.append(NUDGE)
            if acts:
                out.append({"row": row, "acts": acts})
        return out
