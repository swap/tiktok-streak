import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PASS_WEB = "https://www.tiktok.com/passport/web/account/info/"
PASS_API = "https://api.tiktokv.com/passport/account/info/v2/"
PAGE = "https://www.tiktok.com/foryou"
ROOT = Path(__file__).resolve().parent.parent
KEYS = [
    "STREAK_MESSAGE",
    "TIKTOK_SESSIONID",
    "TIKTOK_SESSIONID_SS",
    "TIKTOK_SID_TT",
    "TIKTOK_SID_GUARD",
    "TIKTOK_UID_TT",
    "TIKTOK_UID_TT_SS",
    "TIKTOK_TTWID",
    "TIKTOK_MSTOKEN",
    "TIKTOK_ODIN_TT",
    "TIKTOK_MULTI_SIDS",
    "TIKTOK_PASSPORT_CSRF_TOKEN",
    "TIKTOK_PASSPORT_CSRF_TOKEN_DEFAULT",
]
COOKIES = {
    "sessionid": "TIKTOK_SESSIONID",
    "sessionid_ss": "TIKTOK_SESSIONID_SS",
    "sid_tt": "TIKTOK_SID_TT",
    "sid_guard": "TIKTOK_SID_GUARD",
    "uid_tt": "TIKTOK_UID_TT",
    "uid_tt_ss": "TIKTOK_UID_TT_SS",
    "ttwid": "TIKTOK_TTWID",
    "msToken": "TIKTOK_MSTOKEN",
    "odin_tt": "TIKTOK_ODIN_TT",
    "multi_sids": "TIKTOK_MULTI_SIDS",
    "passport_csrf_token": "TIKTOK_PASSPORT_CSRF_TOKEN",
    "passport_csrf_token_default": "TIKTOK_PASSPORT_CSRF_TOKEN_DEFAULT",
}
ENV2C = {v: k for k, v in COOKIES.items()}

def env(key, default=""):
    return (os.environ.get(key) or default).strip().strip('"')

def as_int(key):
    raw = env(key)
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        return None

def read_env(path):
    path = Path(path)
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        out[k.strip()] = v
    return out

def write_env(path, data):
    Path(path).write_text("\n".join(f"{k}={data.get(k, '')}" for k in KEYS) + "\n", encoding="utf-8")

def jar(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return {}
    return {
        item["name"]: str(item["value"])
        for item in data
        if item.get("name") in COOKIES and item.get("value") is not None
    }

def fill_env(folder=None):
    folder = Path(folder or ROOT)
    path = folder / ".env"
    data = read_env(path)
    data.setdefault("STREAK_MESSAGE", "streak")
    if not data.get("STREAK_MESSAGE"):
        data["STREAK_MESSAGE"] = "streak"
    if not any(k.startswith("TIKTOK_") and not data.get(k) for k in KEYS):
        return data, False
    cookie_path = None
    for p in [folder / "cookies.json", *folder.glob("*.json")]:
        if not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "name" in raw[0]:
            cookie_path = p
            break
    if not cookie_path:
        return data, False
    bag = jar(cookie_path)
    changed = False
    for cname, ekey in COOKIES.items():
        if bag.get(cname) and not data.get(ekey):
            data[ekey] = bag[cname]
            changed = True
    if changed:
        write_env(path, data)
    return data, changed

def load_env(folder=None):
    folder = Path(folder or ROOT)
    data, _ = fill_env(folder)
    data.update(read_env(folder / ".env"))
    if data.get("streak_message") and not data.get("STREAK_MESSAGE"):
        data["STREAK_MESSAGE"] = data.get("streak_message") or ""
    for k in KEYS:
        cur = (os.environ.get(k) or "").strip().strip('"').strip("'")
        if cur:
            continue
        v = (data.get(k) or "").strip().strip('"').strip("'")
        if v:
            os.environ[k] = v
    return {k: env(k) for k in KEYS}

def jar_env():
    return {cname: env(ekey) for ekey, cname in ENV2C.items() if env(ekey)}

class Auth:
    def __init__(self, cookies=None):
        self.cookies = {k: v for k, v in (cookies or jar_env()).items() if v}
        self.user = {}
        self.client = httpx.Client(timeout=30.0, follow_redirects=True, headers=self.base())

    def base(self):
        h = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
        }
        if self.cookies.get("msToken"):
            h["X-Ms-Token"] = self.cookies["msToken"]
        return h

    def cstr(self):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    def headers(self, extra=None):
        h = dict(self.base())
        h["Cookie"] = self.cstr()
        if extra:
            h.update(extra)
        return h

    def did(self):
        ttwid = self.cookies.get("ttwid", "")
        if not ttwid:
            return str(random.randint(7000000000000000000, 7999999999999999999))
        try:
            return ttwid.replace("1%7C", "").split("%7C")[0]
        except Exception:
            return str(random.randint(7000000000000000000, 7999999999999999999))

    def params(self, extra=None):
        base = {
            "aid": "1988",
            "app_language": "en",
            "app_name": "tiktok_web",
            "browser_language": "en-US",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "device_id": self.did(),
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "user",
            "history_len": "4",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "os": "windows",
            "priority_region": "US",
            "referer": "",
            "region": "US",
            "screen_height": "1080",
            "screen_width": "1920",
            "tz_name": "America/New_York",
            "webcast_language": "en",
        }
        if self.cookies.get("msToken"):
            base["msToken"] = self.cookies["msToken"]
        if extra:
            base.update(extra)
        q = urlencode(sorted(base.items()))
        base["X-Bogus"] = hashlib.md5(f"{q}{int(time.time())}{UA}".encode()).hexdigest()[:21]
        return base

    def ok_sess(self):
        return bool(self.cookies.get("sessionid") or self.cookies.get("sid_tt"))

    def from_pass(self, url=None):
        url = url or PASS_API
        try:
            r = self.client.get(url, params={"aid": "1459", "app_name": "tiktok_web"}, headers=self.headers())
            data = r.json().get("data") or {}
            if data.get("error_code") or data.get("name") == "session_expired":
                return False
            uid = data.get("user_id_str") or data.get("user_id")
            if not uid and not data.get("username"):
                return False
            self.user = {
                "id": str(uid) if uid else "",
                "username": data.get("username") or "",
                "nickname": data.get("screen_name") or data.get("name") or "",
                "secUid": data.get("sec_user_id") or "",
            }
            return True
        except Exception:
            return False

    def from_page(self):
        try:
            r = self.client.get(PAGE, headers=self.headers({"Accept": "text/html"}))
            m = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
                r.text,
            )
            if not m:
                return False
            user = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {}).get("webapp.app-context", {}).get("user")
            if not isinstance(user, dict) or not user.get("uid"):
                return False
            self.user = {
                "id": str(user["uid"]),
                "username": user.get("uniqueId") or "",
                "nickname": user.get("nickName") or user.get("nickname") or "",
                "secUid": user.get("secUid") or "",
            }
            return True
        except Exception:
            return False

    def from_sids(self):
        mid = self.cookies.get("multi_sids") or ""
        uid = ""
        if "%3A" in mid:
            uid = mid.split("%3A", 1)[0]
        elif ":" in mid:
            uid = mid.split(":", 1)[0]
        if not uid.isdigit():
            return False
        self.user = {"id": uid, "username": "", "nickname": "", "secUid": ""}
        return True

    def login(self):
        if not self.ok_sess():
            return False
        if self.from_pass(PASS_API):
            return True
        if self.from_pass(PASS_WEB):
            return True
        if self.from_page():
            return True
        return self.from_sids()

    def close(self):
        self.client.close()

def load_auth(folder=None):
    folder = Path(folder or ROOT)
    load_env(folder)
    return Auth(cookies=jar_env())
