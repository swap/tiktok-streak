import json
import time
import uuid
from .auth import Auth
from .guard import heads, load as load_guard

API = "https://api.tiktokv.com"
IM = "https://im-api-sg.tiktok.com"
INIT = f"{API}/v2/message/get_by_user_init"
SEND = f"{IM}/v1/message/send"
SEND_QS = {"aid": "1988", "version_code": "1.0.0", "app_name": "tiktok_web", "device_platform": "web_pc"}
SDK = "1.8.5"
BUILD = "feffaa1:master"
SKIP = ("rate", "limit", "banned", "forbid", "block", "spam", "risk", "verify")
HDR = {
    "Content-Type": "application/x-protobuf",
    "Accept": "application/x-protobuf",
    "Referer": "https://www.tiktok.com/messages",
}

def _vb(n):
    out = bytearray()
    n = int(n) & ((1 << 64) - 1)
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)

def _tag(f, w):
    return _vb((f << 3) | w)

def _vi(f, v):
    return _tag(f, 0) + _vb(int(v))

def _ld(f, data):
    data = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
    return _tag(f, 2) + _vb(len(data)) + bytes(data)

def _str(f, s):
    return _ld(f, str(s).encode())

def _kv(f, k, v):
    return _ld(f, _str(1, k) + _str(2, v))

def _pb(buf):
    out = {}
    i = 0
    while i < len(buf):
        key = shift = 0
        while i < len(buf):
            b = buf[i]
            i += 1
            key |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        f, w = key >> 3, key & 7
        if w == 0:
            val = shift = 0
            while i < len(buf):
                b = buf[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            out.setdefault(f, []).append(val)
        elif w == 2:
            n = shift = 0
            while i < len(buf):
                b = buf[i]
                i += 1
                n |= (b & 0x7F) << shift
                if not b & 0x80:
                    break
                shift += 7
            out.setdefault(f, []).append(buf[i : i + n])
            i += n
        elif w == 1:
            i += 8
        elif w == 5:
            i += 4
        else:
            break
    return out

def _one(d, k, default=None):
    vs = d.get(k) or []
    return vs[0] if vs else default

def _txt(v):
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode()
        except Exception:
            return ""
    return "" if v is None else str(v)

def _ack(buf, http):
    http = int(http)
    code = http
    text = ""
    raw = None
    head = bytes(buf or b"").lstrip()[:1]
    if head in (b"{", b"["):
        try:
            data = json.loads(buf.decode())
        except Exception:
            return code, f"http {http}"
        if not isinstance(data, dict):
            return code, f"http {http}"
        raw = data.get("status_code")
        text = str(data.get("status_msg") or "")
        if raw is None:
            return code, text or f"http {http}"
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return http, text or f"http {http}"
        return code, text or ("OK" if code == 0 else f"code {code}")
    env = _pb(buf)
    raw = _one(env, 3)
    text = _txt(_one(env, 4))
    cmd = _one(env, 1)
    body = _one(env, 6)
    inner = {}
    if isinstance(body, (bytes, bytearray)):
        inner = _pb(_one(_pb(body), 100, b"") or b"")
    ist = _one(inner, 3)
    imsg = _txt(_one(inner, 6))
    if raw is None:
        if text and text != "OK":
            code = http
        elif http == 200 and (cmd is not None or body is not None):
            code = 0
        else:
            code = http
    else:
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return http, text or f"http {http}"
    if isinstance(ist, int) and 0 < ist < 10000000 and code == 0:
        code = ist
        if imsg:
            text = imsg
    return code, text or ("OK" if code == 0 else f"code {code}")

class Inbox:
    def __init__(self, auth=None):
        self.auth = auth or Auth()
        self.last = {}
        self.convs = {}
        self.seq = int(time.time() * 1000) % 2147483647

    def _seq(self):
        self.seq = (self.seq + 1) % 2147483647
        return self.seq

    def _pack(self, cmd, field, body):
        return b"".join(
            [
                _vi(1, cmd),
                _vi(2, self._seq()),
                _str(3, "1.2.0"),
                _str(4, ""),
                _vi(6, 0),
                _ld(8, _ld(field, body)),
                _str(9, self.auth.did()),
                _str(10, "tiktok_web"),
                _str(11, "web"),
                _kv(15, "aid", "1988"),
                _kv(15, "app_name", "tiktok_web"),
                _kv(15, "device_platform", "web"),
            ]
        )

    def load(self, force=False):
        if self.convs and not force:
            return self.convs
        body = _vi(1, 0) + _vi(2, 0) + _vi(3, 0) + _vi(4, 1)
        r = self.auth.client.post(
            INIT,
            params=self.auth.params(),
            content=self._pack(203, 203, body),
            headers=self.auth.headers(HDR),
        )
        if r.status_code != 200 or not r.content:
            return self.convs
        env = _pb(r.content)
        if _one(env, 3) not in (0, None) and _txt(_one(env, 4)) != "OK":
            return self.convs
        inner = _pb(_one(_pb(_one(env, 6, b"") or b""), 203, b"") or b"")
        out = {}
        for chunk in inner.get(2) or []:
            if not isinstance(chunk, (bytes, bytearray)):
                continue
            c = _pb(chunk)
            cid = _txt(_one(c, 1, b""))
            if not cid:
                continue
            out[cid] = {
                "cid": cid,
                "short": int(_one(c, 2) or 0),
                "type": int(_one(c, 3) or 1),
                "ticket": _txt(_one(c, 4, b"")) or "deprecated",
            }
        self.convs = out
        return out

    def _conv(self, peer, cid="", ctype=1):
        peer = str(peer or "")
        cid = str(cid or "")
        ctype = int(ctype or 1)
        my = str(self.auth.user.get("id") or "")
        if ctype == 2 and cid:
            short = int(cid) if cid.isdigit() else 0
            self.load()
            if cid in self.convs:
                return self.convs[cid]
            return {"cid": cid, "short": short, "type": 2, "ticket": "deprecated"}
        self.load()
        want = [x for x in (cid, f"0:1:{peer}:{my}", f"0:1:{my}:{peer}") if x]
        for key in want:
            if key in self.convs:
                return self.convs[key]
        self.load(force=True)
        for key in want:
            if key in self.convs:
                return self.convs[key]
        if my and peer:
            return {"cid": f"0:1:{peer}:{my}", "short": 0, "type": 1, "ticket": "deprecated"}
        return None

    def send(self, peer, text, conversation_id="", conv_type=1):
        peer = str(peer or "")
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "code": -2, "msg": "empty", "skip": False}
        if not self.auth.user.get("id") and not self.auth.login():
            return {"ok": False, "code": -3, "msg": "login", "skip": False}
        ctype = int(conv_type or 1)
        cid = str(conversation_id or "")
        if ctype == 2 and not cid and peer.isdigit():
            cid = peer
        info = self._conv(peer, cid, ctype)
        if not info:
            return {"ok": False, "code": -4, "msg": "no conv", "skip": False}
        mid = str(uuid.uuid4())
        guard = load_guard()
        if not guard:
            return {"ok": False, "code": -8, "msg": "no guard", "skip": False}
        content = json.dumps({"aweType": 0, "text": text}, separators=(",", ":"))
        body = b"".join(
            [
                _str(1, info["cid"]),
                _vi(2, int(info["type"] or 1)),
                _vi(3, int(info["short"] or 0)),
                _str(4, content),
                _ld(5, _str(1, "s:mentioned_users") + _str(2, "")),
                _ld(5, _str(1, "s:client_message_id") + _str(2, mid)),
                _vi(6, 7),
                _str(7, "deprecated"),
                _str(8, mid),
            ]
        )
        did = self.auth.did()
        frame = b"".join(
            [
                _vi(1, 100),
                _vi(2, self._seq()),
                _str(3, SDK),
                _str(4, ""),
                _vi(5, 3),
                _vi(6, 0),
                _str(7, BUILD),
                _ld(8, _ld(100, body)),
                _str(9, did),
                _str(11, "web"),
                _kv(15, "aid", "1988"),
                _kv(15, "app_name", "tiktok_web"),
                _kv(15, "channel", "web"),
                _kv(15, "device_platform", "web_pc"),
                _kv(15, "device_id", did),
                _kv(15, "user_is_login", "true"),
                _vi(18, 1),
            ]
        )
        headers = self.auth.headers(HDR)
        headers.update(heads(guard, "/v1/message/send"))
        try:
            r = self.auth.client.post(SEND, params=SEND_QS, content=frame, headers=headers)
        except Exception as e:
            out = {"ok": False, "code": -1, "msg": f"network error: {e}", "skip": False}
            self.last[peer or cid] = {"at": int(time.time()), **out, "text": text}
            return out
        if r.status_code in (401, 403):
            out = {"ok": False, "code": -3, "msg": "login", "skip": False}
            self.last[peer or cid] = {"at": int(time.time()), **out, "text": text}
            return out
        if not r.content:
            out = {"ok": False, "code": -7, "msg": "API response invalid", "skip": False}
            self.last[peer or cid] = {"at": int(time.time()), **out, "text": text}
            return out
        try:
            code, msg = _ack(r.content, r.status_code)
        except (TypeError, ValueError):
            out = {"ok": False, "code": -7, "msg": "API response invalid", "skip": False}
            self.last[peer or cid] = {"at": int(time.time()), **out, "text": text}
            return out
        skip = any(x in msg.lower() for x in SKIP) or code in (2154, 3002325, 3002282)
        out = {"ok": code == 0 or msg == "OK", "code": code, "msg": msg, "skip": skip}
        self.last[peer or cid] = {"at": int(time.time()), **out, "text": text}
        return out
