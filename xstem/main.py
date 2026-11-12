import asyncio
import hashlib
import json
import os
import platform
import re
import secrets
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import psutil
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import bootstrap, core, virt
from .core import q, q1, x

WEB = Path(__file__).parent / "web"
app = FastAPI(title="xstem panel API", version=core.VERSION, docs_url="/api/swagger", redoc_url=None, openapi_url="/api/openapi.json")
LOCK = threading.Lock()
FAILS = {}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
USER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
psutil.cpu_percent(interval=None)


@app.on_event("startup")
def _startup():
    core.node_token()
    bootstrap.start()


@app.middleware("http")
async def gate(request: Request, call_next):
    p = request.url.path
    if p.startswith("/api/") and bootstrap.STATE["state"] != "done" and not p.startswith(("/api/public", "/api/setup", "/api/swagger", "/api/openapi")):
        return JSONResponse({"detail": "installing dependencies"}, status_code=503)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ---------------------------------------------------------------- auth

def _who(who):
    if who == "root@pam":
        return {"who": who, "name": "root", "admin": True, "root": True}
    name = who.rsplit("@", 1)[0]
    u = q1("select * from users where name=?", name)
    if not u or u["disabled"]:
        return None
    return {"who": who, "name": name, "admin": u["role"] == "admin", "root": False}


def principal(headers, cookies, key=None):
    tok = None
    a = headers.get("authorization", "")
    if a.lower().startswith("bearer "):
        tok = a[7:].strip()
    tok = tok or headers.get("x-api-key") or key
    if tok:
        if secrets.compare_digest(tok, core.node_token()):
            return {"who": "cluster", "name": "cluster", "admin": True, "root": True}
        k = q1("select * from keys where hash=?", core.sha(tok))
        if not k:
            return None
        x("update keys set last=? where id=?", time.time(), k["id"])
        return _who(k["owner"])
    s = cookies.get("xs_session")
    if s:
        r = q1("select * from sessions where tok=? and exp>?", core.sha(s), time.time())
        if r:
            return _who(r["who"])
    return None


def need(request: Request):
    p = principal(request.headers, request.cookies)
    if not p:
        raise HTTPException(401, "authentication required")
    return p


def need_admin(p=Depends(need)):
    if not p["admin"]:
        raise HTTPException(403, "admin only")
    return p


def need_root(p=Depends(need)):
    if not p["root"]:
        raise HTTPException(403, "root@pam only")
    return p


class Login(BaseModel):
    realm: str = "panel"
    username: str
    password: str


@app.post("/api/login")
def login(b: Login, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    n, until = FAILS.get(ip, (0, 0))
    if n >= 8 and time.time() < until:
        raise HTTPException(429, "too many attempts, wait a few minutes")
    who = None
    if b.realm == "pam":
        if b.username == "root" and core.pam_ok("root", b.password):
            who = "root@pam"
    else:
        u = q1("select * from users where name=?", b.username)
        if u and not u["disabled"] and core.check_pw(b.password, u["pw"]):
            who = "%s@panel" % u["name"]
    if not who:
        FAILS[ip] = (n + 1, time.time() + 300)
        core.log("auth", "failed login %s@%s from %s" % (b.username[:32], b.realm[:8], ip))
        raise HTTPException(401, "wrong realm, username or password")
    FAILS.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    x("delete from sessions where exp<?", time.time())
    x("insert into sessions(tok,who,exp) values(?,?,?)", core.sha(tok), who, time.time() + 7 * 86400)
    response.set_cookie("xs_session", tok, httponly=True, secure=True, samesite="strict", max_age=7 * 86400)
    core.log(who, "logged in from %s" % ip)
    return _who(who)


@app.post("/api/logout")
def logout(request: Request, response: Response):
    s = request.cookies.get("xs_session")
    if s:
        x("delete from sessions where tok=?", core.sha(s))
    response.delete_cookie("xs_session")
    return {"ok": True}


@app.get("/api/me")
def me(p=Depends(need)):
    return p


@app.get("/api/public")
def public():
    keys = ("brand", "accent", "mode", "bg_type", "bg_url", "dim")
    d = {k: core.setting(k) for k in keys}
    d["version"] = core.VERSION
    d["setup"] = {k: bootstrap.STATE[k] for k in ("state", "step", "pct")}
    return d


@app.get("/api/setup/status")
def setup_status():
    return bootstrap.STATE


@app.post("/api/setup/retry")
def setup_retry():
    if bootstrap.STATE["state"] == "error":
        bootstrap.start()
    return {"ok": True}


# ---------------------------------------------------------------- node

def node_stats():
    vm, sw, du = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage(str(core.DATA))
    try:
        osname = next(l.split("=", 1)[1].strip().strip('"') for l in open("/etc/os-release") if l.startswith("PRETTY_NAME="))
    except Exception:
        osname = platform.system()
    gs = q("select kind,state from guests")
    running = sum(1 for g in q("select * from guests where state='ready'") if virt.status(g) == "running")
    return {"hostname": socket.gethostname(), "os": osname, "kernel": platform.release(), "cpus": psutil.cpu_count(),
            "cpu": psutil.cpu_percent(interval=None), "load": list(psutil.getloadavg()), "mem_used": vm.used, "mem_total": vm.total,
            "swap_used": sw.used, "swap_total": sw.total, "disk_used": du.used, "disk_total": du.total,
            "uptime": int(time.time() - psutil.boot_time()), "kvm": os.path.exists("/dev/kvm"),
            "vms": sum(1 for g in gs if g["kind"] == "vm"), "cts": sum(1 for g in gs if g["kind"] == "ct"),
            "running": running, "version": core.VERSION}


@app.get("/api/node/stats")
def node_stats_route(p=Depends(need_admin)):
    return node_stats()


@app.get("/api/node/info")
def node_info(p=Depends(need_admin)):
    return {"hostname": socket.gethostname(), "version": core.VERSION}


@app.get("/api/node/token")
def node_token_get(p=Depends(need_root)):
    return {"token": core.node_token()}


@app.post("/api/node/token")
def node_token_new(p=Depends(need_root)):
    core.put("node_token", "xn_" + secrets.token_urlsafe(24))
    core.log(p["who"], "regenerated node token")
    return {"token": core.node_token()}


# ---------------------------------------------------------------- guests

class NewGuest(BaseModel):
    kind: str
    name: str
    image: str
    cores: int = 1
    ram: int = 1024
    disk: int = 10
    password: str = ""
    owner: str = ""
    autostart: bool = False


class Power(BaseModel):
    action: str


class PatchGuest(BaseModel):
    autostart: Optional[bool] = None
    owner: Optional[str] = None


class Forward(BaseModel):
    guest_port: int


def view(g, stats=False):
    st = virt.status(g)
    d = {k: g[k] for k in ("id", "kind", "name", "owner", "image", "cores", "ram", "disk", "ip", "mac", "autostart", "iso", "state", "error", "created")}
    d["forwards"] = [{"host": a, "guest": b} for a, b in json.loads(g["forwards"])]
    d["status"] = st
    if stats:
        d["stats"] = virt.sample(g, st)
    return d


def getg(ref, p):
    g = q1("select * from guests where id=?", int(ref)) if str(ref).isdigit() else q1("select * from guests where name=?", ref)
    if not g or not (p["admin"] or g["owner"] == p["name"]):
        raise HTTPException(404, "machine not found")
    return g


@app.get("/api/guests")
def guests(stats: int = 0, p=Depends(need)):
    rows = q("select * from guests order by name") if p["admin"] else q("select * from guests where owner=? order by name", p["name"])
    return [view(g, bool(stats)) for g in rows]


@app.post("/api/guests")
def create_guest(b: NewGuest, p=Depends(need_admin)):
    if b.kind not in ("vm", "ct"):
        raise HTTPException(400, "kind must be vm or ct")
    if not NAME_RE.match(b.name):
        raise HTTPException(400, "name: 2-31 chars, lowercase letters, digits, dashes, start with a letter")
    if not 1 <= b.cores <= (psutil.cpu_count() or 1) * 4:
        raise HTTPException(400, "cores out of range")
    if not 128 <= b.ram <= max(256, psutil.virtual_memory().total // 1048576):
        raise HTTPException(400, "ram must be 128 MB up to the host total")
    if not 1 <= b.disk <= 4096:
        raise HTTPException(400, "disk must be 1-4096 GB")
    if b.owner and not q1("select id from users where name=?", b.owner):
        raise HTTPException(400, "owner does not exist")
    if b.kind == "vm":
        im = q1("select * from images where key=?", b.image)
        if not im or im["state"] != "ready":
            raise HTTPException(400, "image is not downloaded, open Images first")
    elif not re.match(r"^[a-z0-9]+/[a-z0-9._-]+$", b.image):
        raise HTTPException(400, "container template looks like debian/bookworm")
    pw = b.password or secrets.token_urlsafe(9)
    if len(pw) < 6 or "\n" in pw:
        raise HTTPException(400, "password needs 6+ characters")
    with LOCK:
        if q1("select id from guests where name=?", b.name):
            raise HTTPException(409, "name already used")
        iso = 1 if b.kind == "vm" and im["kind"] == "iso" else 0
        gid = x("insert into guests(kind,name,owner,image,cores,ram,disk,mac,ip,forwards,autostart,iso,created) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                b.kind, b.name, b.owner, b.image, b.cores, b.ram, b.disk, virt.new_mac(), virt.next_ip(), "[]", int(b.autostart), iso, time.time())
        try:
            x("update guests set forwards=? where id=?", json.dumps([[virt.free_port(), 22]]), gid)
        except RuntimeError:
            pass
    threading.Thread(target=virt.provision, args=(gid, pw), daemon=True).start()
    core.log(p["who"], "created %s %s" % (b.kind, b.name))
    return {"id": gid, "name": b.name, "password": pw}


@app.get("/api/guests/{ref}")
def guest(ref: str, p=Depends(need)):
    return view(getg(ref, p), True)


@app.get("/api/guests/{ref}/stats")
def guest_stats(ref: str, p=Depends(need)):
    g = getg(ref, p)
    return virt.sample(g, virt.status(g))


@app.patch("/api/guests/{ref}")
def patch_guest(ref: str, b: PatchGuest, p=Depends(need_admin)):
    g = getg(ref, p)
    if b.autostart is not None:
        x("update guests set autostart=? where id=?", int(b.autostart), g["id"])
    if b.owner is not None:
        if b.owner and not q1("select id from users where name=?", b.owner):
            raise HTTPException(400, "owner does not exist")
        x("update guests set owner=? where id=?", b.owner, g["id"])
    return view(getg(g["id"], p))


@app.delete("/api/guests/{ref}")
def delete_guest(ref: str, p=Depends(need_admin)):
    g = getg(ref, p)
    virt.destroy(g)
    core.log(p["who"], "deleted %s %s" % (g["kind"], g["name"]))
    return {"ok": True}


@app.post("/api/guests/{ref}/power")
def guest_power(ref: str, b: Power, p=Depends(need)):
    g = getg(ref, p)
    if g["state"] != "ready":
        raise HTTPException(409, "machine is %s" % g["state"])
    if b.action not in ("start", "shutdown", "stop", "reboot"):
        raise HTTPException(400, "action: start, shutdown, stop or reboot")
    try:
        virt.power(g, b.action)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    core.log(p["who"], "%s %s" % (b.action, g["name"]))
    return {"ok": True}


@app.post("/api/guests/{ref}/eject")
def eject(ref: str, p=Depends(need)):
    g = getg(ref, p)
    x("update guests set iso=0 where id=?", g["id"])
    return {"ok": True}


@app.post("/api/guests/{ref}/forwards")
def add_forward(ref: str, b: Forward, p=Depends(need)):
    g = getg(ref, p)
    fw = json.loads(g["forwards"])
    if not 1 <= b.guest_port <= 65535:
        raise HTTPException(400, "port out of range")
    if len(fw) >= 12:
        raise HTTPException(400, "12 forwards per machine")
    with LOCK:
        try:
            fw.append([virt.free_port(), b.guest_port])
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        x("update guests set forwards=? where id=?", json.dumps(fw), g["id"])
    virt.apply_forwards()
    return view(getg(g["id"], p))


@app.delete("/api/guests/{ref}/forwards/{host}")
def del_forward(ref: str, host: int, p=Depends(need)):
    g = getg(ref, p)
    fw = json.loads(g["forwards"])
    keep = [f for f in fw if not (f[0] == host and f[1] != 22)]
    x("update guests set forwards=? where id=?", json.dumps(keep), g["id"])
    virt.apply_forwards()
    return view(getg(g["id"], p))


# ---------------------------------------------------------------- images

class NewImage(BaseModel):
    name: str
    url: str
    kind: str = "cloud"


@app.get("/api/images")
def images(p=Depends(need)):
    out = q("select id,key,name,kind,size,got,state,error from images order by kind desc, name")
    return {"images": out, "templates": virt.CT_TEMPLATES}


@app.post("/api/images")
def add_image(b: NewImage, p=Depends(need_admin)):
    if b.kind not in ("cloud", "iso") or not re.match(r"^https?://\S+$", b.url) or not 1 <= len(b.name) <= 60:
        raise HTTPException(400, "need a name, an http(s) URL and kind cloud or iso")
    key = "custom-%s" % secrets.token_hex(4)
    iid = x("insert into images(key,name,kind,url,file) values(?,?,?,?,?)", key, b.name, b.kind, b.url, key + (".iso" if b.kind == "iso" else ".img"))
    virt.start_download(iid)
    core.log(p["who"], "added image %s" % b.name)
    return {"id": iid}


@app.post("/api/images/{iid}/download")
def image_download(iid: int, p=Depends(need_admin)):
    im = q1("select * from images where id=?", iid)
    if not im:
        raise HTTPException(404, "image not found")
    if im["state"] == "downloading":
        raise HTTPException(409, "already downloading")
    virt.start_download(iid)
    return {"ok": True}


@app.delete("/api/images/{iid}")
def image_delete(iid: int, p=Depends(need_admin)):
    im = q1("select * from images where id=?", iid)
    if not im:
        raise HTTPException(404, "image not found")
    if q1("select id from guests where image=?", im["key"]):
        raise HTTPException(409, "image is used by a machine")
    (core.IMAGES / im["file"]).unlink(missing_ok=True)
    if im["key"].startswith("custom-"):
        x("delete from images where id=?", iid)
    else:
        x("update images set state='new',got=0,size=0,error='' where id=?", iid)
    return {"ok": True}


# ---------------------------------------------------------------- users, keys, journal

class NewUser(BaseModel):
    name: str
    password: str
    role: str = "user"


class PatchUser(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    disabled: Optional[bool] = None


class NewKey(BaseModel):
    name: str = "key"


@app.get("/api/users")
def users(p=Depends(need_root)):
    return q("select u.id,u.name,u.role,u.disabled,u.created,(select count(*) from guests g where g.owner=u.name) as machines from users u order by u.name")


@app.post("/api/users")
def create_user(b: NewUser, p=Depends(need_root)):
    if not USER_RE.match(b.name) or b.role not in ("user", "admin") or len(b.password) < 8:
        raise HTTPException(400, "name: lowercase letters, digits, - _ ; role user or admin; password 8+ characters")
    if q1("select id from users where name=?", b.name):
        raise HTTPException(409, "user exists")
    x("insert into users(name,pw,role,created) values(?,?,?,?)", b.name, core.hash_pw(b.password), b.role, time.time())
    core.log(p["who"], "created user %s" % b.name)
    return {"ok": True}


@app.patch("/api/users/{name}")
def patch_user(name: str, b: PatchUser, p=Depends(need_root)):
    if not q1("select id from users where name=?", name):
        raise HTTPException(404, "user not found")
    if b.password is not None:
        if len(b.password) < 8:
            raise HTTPException(400, "password needs 8+ characters")
        x("update users set pw=? where name=?", core.hash_pw(b.password), name)
    if b.role in ("user", "admin"):
        x("update users set role=? where name=?", b.role, name)
    if b.disabled is not None:
        x("update users set disabled=? where name=?", int(b.disabled), name)
        if b.disabled:
            x("delete from sessions where who=?", name + "@panel")
    core.log(p["who"], "updated user %s" % name)
    return {"ok": True}


@app.delete("/api/users/{name}")
def delete_user(name: str, p=Depends(need_root)):
    x("delete from users where name=?", name)
    x("update guests set owner='' where owner=?", name)
    x("delete from keys where owner=?", name + "@panel")
    x("delete from sessions where who=?", name + "@panel")
    core.log(p["who"], "deleted user %s" % name)
    return {"ok": True}


@app.get("/api/keys")
def keys(p=Depends(need)):
    if p["who"] == "cluster":
        return []
    return q("select id,name,prefix,created,last from keys where owner=? order by id", p["who"])


@app.post("/api/keys")
def create_key(b: NewKey, p=Depends(need)):
    if p["who"] == "cluster":
        raise HTTPException(400, "log in as a user to create keys")
    key = "xs_" + secrets.token_urlsafe(30)
    x("insert into keys(owner,name,hash,prefix,created) values(?,?,?,?,?)", p["who"], b.name[:40], core.sha(key), key[:9], time.time())
    return {"key": key}


@app.delete("/api/keys/{kid}")
def delete_key(kid: int, p=Depends(need)):
    x("delete from keys where id=? and owner=?", kid, p["who"])
    return {"ok": True}


@app.get("/api/events")
def events(p=Depends(need_admin)):
    return q("select ts,who,msg from events order by id desc limit 100")


# ---------------------------------------------------------------- theme

class Theme(BaseModel):
    brand: str
    accent: str
    mode: str
    dim: float
    css: str = ""
    bg_type: str = "none"
    bg_url: str = ""


@app.get("/api/settings")
def settings_get(p=Depends(need_root)):
    return {k: core.setting(k) for k in core.DEFAULTS}


@app.put("/api/settings")
def settings_put(b: Theme, p=Depends(need_root)):
    if not re.match(r"^#[0-9a-fA-F]{6}$", b.accent) or b.mode not in ("dark", "light") or not 0 <= b.dim <= 1 or b.bg_type not in ("none", "image", "video"):
        raise HTTPException(400, "invalid theme values")
    if b.bg_url and not re.match(r"^(/bg/[\w.-]+|https?://[^\s'\"()]+)$", b.bg_url):
        raise HTTPException(400, "background must be an uploaded file or an http(s) URL")
    if len(b.css) > 20000 or not 1 <= len(b.brand) <= 30:
        raise HTTPException(400, "brand 1-30 chars, css under 20 KB")
    for k, v in (("brand", b.brand), ("accent", b.accent), ("mode", b.mode), ("dim", b.dim), ("css", b.css), ("bg_type", b.bg_type), ("bg_url", b.bg_url)):
        core.put(k, v)
    core.log(p["who"], "changed appearance")
    return {"ok": True}


@app.put("/api/settings/background")
async def upload_bg(request: Request, name: str, p=Depends(need_root)):
    ext = Path(name).suffix.lower()
    kinds = {".mp4": "video", ".webm": "video", ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".gif": "image"}
    if ext not in kinds:
        raise HTTPException(400, "allowed: mp4 webm jpg png webp gif")
    if int(request.headers.get("content-length") or 0) > 150 * 1048576:
        raise HTTPException(413, "max 150 MB")
    fname = "bg-%s%s" % (secrets.token_hex(4), ext)
    size = 0
    with open(core.ASSETS / fname, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > 150 * 1048576:
                break
            f.write(chunk)
    if size > 150 * 1048576:
        (core.ASSETS / fname).unlink(missing_ok=True)
        raise HTTPException(413, "max 150 MB")
    for old in core.ASSETS.glob("bg-*"):
        if old.name != fname:
            old.unlink(missing_ok=True)
    core.put("bg_type", kinds[ext])
    core.put("bg_url", "/bg/" + fname)
    return {"url": "/bg/" + fname, "type": kinds[ext]}


@app.get("/bg/{name}")
def bg(name: str):
    f = core.ASSETS / Path(name).name
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f)


@app.get("/theme.css")
def theme_css():
    css = ":root{--accent:%s;--dim:%s}\n%s" % (core.setting("accent"), core.setting("dim"), core.setting("css"))
    return PlainTextResponse(css, media_type="text/css")


# ---------------------------------------------------------------- nodes (cluster)

class NewNode(BaseModel):
    name: str
    url: str
    token: str


def _ctx(pem):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_REQUIRED
    c.load_verify_locations(cadata=pem)
    return c


def _fetch_cert(host, port):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), 8) as raw, c.wrap_socket(raw) as t:
        return ssl.DER_cert_to_PEM_cert(t.getpeercert(True))


def _call(n, method, path, body=None, ctype=None, timeout=20):
    headers = {"authorization": "Bearer " + n["token"]}
    if ctype:
        headers["content-type"] = ctype
    with httpx.Client(verify=_ctx(n["cert"]), timeout=timeout) as c:
        return c.request(method, n["url"] + path, content=body, headers=headers)


def _fp(pem):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


@app.get("/api/nodes")
def nodes(stats: int = 1, p=Depends(need_root)):
    out = [{"id": 0, "name": socket.gethostname(), "url": "this server", "online": True, "stats": node_stats() if stats else None}]
    rows = q("select * from nodes order by name")

    def one(n):
        d = {"id": n["id"], "name": n["name"], "url": n["url"], "fingerprint": _fp(n["cert"]), "online": False, "stats": None}
        if stats:
            try:
                r = _call(n, "GET", "/api/node/stats", timeout=4)
                if r.status_code == 200:
                    d["online"], d["stats"] = True, r.json()
            except Exception:
                pass
        return d

    with ThreadPoolExecutor(max_workers=8) as ex:
        out += list(ex.map(one, rows))
    return out


@app.post("/api/nodes")
def add_node(b: NewNode, p=Depends(need_root)):
    u = urlparse(b.url if "://" in b.url else "https://" + b.url)
    if not u.hostname or not NAME_RE.match(b.name):
        raise HTTPException(400, "need a node name (a-z, 0-9, -) and its panel address")
    url = "https://%s:%d" % (u.hostname if ":" not in u.hostname else "[%s]" % u.hostname, u.port or 8443)
    if q1("select id from nodes where name=?", b.name):
        raise HTTPException(409, "node name exists")
    try:
        pem = _fetch_cert(u.hostname, u.port or 8443)
        n = {"url": url, "token": b.token.strip(), "cert": pem}
        r = _call(n, "GET", "/api/node/info", timeout=8)
    except Exception as e:
        raise HTTPException(502, "cannot reach node: %s" % e)
    if r.status_code != 200:
        raise HTTPException(400, "node refused the token (HTTP %d)" % r.status_code)
    nid = x("insert into nodes(name,url,token,cert) values(?,?,?,?)", b.name, url, n["token"], pem)
    core.log(p["who"], "linked node %s" % b.name)
    return {"id": nid, "fingerprint": _fp(pem)}


@app.delete("/api/nodes/{nid}")
def del_node(nid: int, p=Depends(need_root)):
    x("delete from nodes where id=?", nid)
    return {"ok": True}


@app.api_route("/api/n/{nid}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def node_proxy(nid: int, path: str, request: Request, p=Depends(need_root)):
    n = q1("select * from nodes where id=?", nid)
    if not n:
        raise HTTPException(404, "node not found")
    qs = ("?" + request.url.query) if request.url.query else ""
    body = await request.body()
    try:
        r = await run_in_threadpool(_call, n, request.method, "/api/" + path + qs, body or None, request.headers.get("content-type"), 60)
    except Exception as e:
        raise HTTPException(502, "node unreachable: %s" % e)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))


# ---------------------------------------------------------------- consoles

def _ws_principal(ws: WebSocket):
    return principal(ws.headers, ws.cookies, ws.query_params.get("key"))


@app.websocket("/ws/console/{gid}")
async def ws_console(ws: WebSocket, gid: int):
    p = _ws_principal(ws)
    g = q1("select * from guests where id=?", gid)
    if not p or not g or not (p["admin"] or g["owner"] == p["name"]):
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        con = await virt.open_console(g)
    except Exception as e:
        await ws.send_text("\r\n\x1b[31m%s\x1b[0m\r\n" % e)
        await ws.close()
        return

    async def pump():
        while True:
            d = await con.read()
            if not d:
                break
            await ws.send_bytes(d)
        await ws.close()

    task = asyncio.create_task(pump())
    try:
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                break
            if m.get("text") is not None:
                s = m["text"]
                if s.startswith('{"resize"'):
                    c, r = json.loads(s)["resize"]
                    con.resize(c, r)
                else:
                    con.write(s.encode())
            elif m.get("bytes"):
                con.write(m["bytes"])
    except Exception:
        pass
    finally:
        task.cancel()
        con.close()


@app.websocket("/ws/n/{nid}/console/{gid}")
async def ws_remote_console(ws: WebSocket, nid: int, gid: int):
    p = _ws_principal(ws)
    n = q1("select * from nodes where id=?", nid)
    if not p or not p["root"] or not n:
        await ws.close(code=4403)
        return
    await ws.accept()
    import websockets
    try:
        up = await websockets.connect("wss://%s/ws/console/%d?key=%s" % (n["url"][8:], gid, n["token"]), ssl=_ctx(n["cert"]))
    except Exception as e:
        await ws.send_text("\r\n\x1b[31mnode unreachable: %s\x1b[0m\r\n" % e)
        await ws.close()
        return

    async def down():
        async for m in up:
            await ws.send_bytes(m if isinstance(m, bytes) else m.encode())

    async def upl():
        while True:
            m = await ws.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("text") is not None:
                await up.send(m["text"])
            elif m.get("bytes"):
                await up.send(m["bytes"])

    tasks = [asyncio.create_task(down()), asyncio.create_task(upl())]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in tasks:
        t.cancel()
    await up.close()
    try:
        await ws.close()
    except Exception:
        pass


# ---------------------------------------------------------------- pages

app.mount("/vendor", StaticFiles(directory=str(core.VENDOR)), name="vendor")
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/docs")
def docs_page():
    return FileResponse(WEB / "docs.html")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")
