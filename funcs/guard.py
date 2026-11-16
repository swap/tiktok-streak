import base64
import json
import os
import re
import struct
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

IDB = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "Google/Chrome/User Data/Default/IndexedDB/https_www.tiktok.com_0.indexeddb.leveldb"
ENV = Path(__file__).resolve().parent.parent / ".env"

def _uvar(buf, i):
    n = s = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << s
        if not b & 0x80:
            return n, i
        s += 7
        if s > 63:
            break
    return None, i

def _snap(src):
    ulen, i = _uvar(src, 0)
    out = bytearray()
    while i < len(src):
        tag = src[i]
        i += 1
        kind = tag & 3
        if kind == 0:
            ln = tag >> 2
            if ln < 60:
                length = ln + 1
            else:
                nbytes = ln - 59
                length = int.from_bytes(src[i : i + nbytes], "little") + 1
                i += nbytes
            out += src[i : i + length]
            i += length
        elif kind == 1:
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | src[i]
            i += 1
            start = len(out) - offset
            for k in range(length):
                out.append(out[start + k])
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(src[i : i + 2], "little")
            i += 2
            start = len(out) - offset
            for k in range(length):
                out.append(out[start + k])
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(src[i : i + 4], "little")
            i += 4
            start = len(out) - offset
            for k in range(length):
                out.append(out[start + k])
    if ulen is not None and len(out) != ulen:
        raise ValueError("snappy")
    return bytes(out)

def _block(blob, offset, size):
    raw = blob[offset : offset + size]
    kind = blob[offset + size]
    if kind == 1:
        return _snap(raw)
    return raw

def _recs(block):
    if len(block) < 4:
        return
    n = struct.unpack_from("<I", block, len(block) - 4)[0]
    body = block[: len(block) - 4 - n * 4]
    i = 0
    key = b""
    while i < len(body):
        shared, i = _uvar(body, i)
        unshared, i = _uvar(body, i)
        vlen, i = _uvar(body, i)
        if None in (shared, unshared, vlen):
            return
        frag = body[i : i + unshared]
        i += unshared
        key = key[:shared] + frag
        val = body[i : i + vlen]
        i += vlen
        yield key, val

def _plain(blob):
    footer = blob[-48:]
    _, i = _uvar(footer, 0)
    _, i = _uvar(footer, i)
    off, i = _uvar(footer, i)
    size, i = _uvar(footer, i)
    parts = []
    for _, val in _recs(_block(blob, off, size)):
        o, j = _uvar(val, 0)
        s, j = _uvar(val, j)
        try:
            parts.append(_block(blob, o, s))
        except Exception:
            continue
    return b"\n".join(parts)

def _pem(data):
    m = re.search(br"-----BEGIN PRIVATE KEY-----\\n(.+?)-----END PRIVATE KEY-----", data, re.S)
    if not m:
        m = re.search(br"-----BEGIN PRIVATE KEY-----(.+?)-----END PRIVATE KEY-----", data, re.S)
    if not m:
        return None
    body = m.group(1).decode("utf-8", "replace").replace("\\n", "").replace("\n", "")
    body += "=" * ((4 - len(body) % 4) % 4)
    der = base64.b64decode(body)
    return serialization.load_der_private_key(der, password=None), base64.b64encode(der).decode()

def _ticket(extra):
    try:
        buf = base64.b64decode(extra)
    except Exception:
        return ""
    if len(buf) < 13:
        return ""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=16, salt=b"secure-salt", iterations=1000)
    try:
        return AESGCM(kdf.derive(b"tt-ticket-guard-iv")).decrypt(buf[:12], buf[12:], None).decode()
    except Exception:
        return ""

def _idb():
    if not IDB.is_dir():
        return None
    data = b""
    for p in IDB.glob("*.ldb"):
        try:
            data += _plain(p.read_bytes())
        except Exception:
            continue
    got = _pem(data)
    if not got:
        return None
    key, der = got
    ts_m = re.search(br"ts\.1\.[0-9a-fA-F]{32,}", data)
    if not ts_m:
        return None
    ts = ts_m.group(0).decode()
    tail = data[ts_m.end() : ts_m.end() + 1200]
    extra_m = re.search(br"[A-Za-z0-9+/=]{24,}", tail)
    extra = extra_m.group(0).decode() if extra_m else ""
    ticket = _ticket(extra) or extra
    if not ticket:
        return None
    return {"key": key, "der": der, "ts": ts, "ticket": ticket}

def _env():
    der = os.environ.get("TIKTOK_GUARD_DER", "").strip()
    ts = os.environ.get("TIKTOK_GUARD_TS", "").strip()
    ticket = os.environ.get("TIKTOK_GUARD_TICKET", "").strip()
    if not der or not ts or not ticket:
        return None
    raw = base64.b64decode(der)
    key = serialization.load_der_private_key(raw, password=None)
    return {"key": key, "der": der, "ts": ts, "ticket": ticket}

def load():
    return _env() or _idb()

def heads(g, path):
    stamp = int(time.time())
    msg = f"ticket={g['ticket']}&path={path}&timestamp={stamp}".encode()
    sig = base64.b64encode(g["key"].sign(msg, ec.ECDSA(hashes.SHA256()))).decode()
    client = {"ts_sign": g["ts"], "req_content": "ticket,path,timestamp", "req_sign": sig, "timestamp": stamp}
    pub = g["key"].public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {
        "tt-ticket-guard-client-data": base64.b64encode(json.dumps(client, separators=(",", ":")).encode()).decode(),
        "tt-ticket-guard-public-key": base64.b64encode(pub).decode(),
        "tt-ticket-guard-version": "2",
        "tt-ticket-guard-web-version": "1",
        "tt-ticket-guard-iteration-version": "0",
    }

def save():
    g = _idb()
    if not g or not ENV.is_file():
        return False
    keys = {"TIKTOK_GUARD_DER": g["der"], "TIKTOK_GUARD_TS": g["ts"], "TIKTOK_GUARD_TICKET": g["ticket"]}
    lines = []
    seen = set()
    for line in ENV.read_text(encoding="utf-8").splitlines():
        k = line.split("=", 1)[0] if "=" in line else ""
        if k in keys:
            lines.append(f"{k}={keys[k]}")
            seen.add(k)
        else:
            lines.append(line)
    for k, v in keys.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True
