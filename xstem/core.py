import hashlib
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

VERSION = "1.0.0"
HOME = Path(os.environ.get("XSTEM_HOME", "/opt/xstem"))
DATA = Path(os.environ.get("XSTEM_DATA", "/var/lib/xstem"))
ETC = Path(os.environ.get("XSTEM_ETC", "/etc/xstem"))
RUN = Path(os.environ.get("XSTEM_RUN", "/run/xstem"))
IMAGES, DISKS, LXC, ASSETS, VENDOR = (DATA / n for n in ("images", "vms", "lxc", "assets", "vendor"))
BRIDGE = "xbr0"
SUBNET = "10.77.0"

for _d in (DATA, ETC, RUN, IMAGES, DISKS, LXC, ASSETS, VENDOR):
    _d.mkdir(parents=True, exist_ok=True)

SCHEMA = """
create table if not exists users(id integer primary key, name text unique, pw text, role text default 'user', disabled int default 0, created real);
create table if not exists sessions(tok text primary key, who text, exp real);
create table if not exists keys(id integer primary key, owner text, name text, hash text unique, prefix text, created real, last real default 0);
create table if not exists guests(id integer primary key, kind text, name text unique, owner text default '', image text, cores int, ram int, disk int,
  mac text, ip text, forwards text default '[]', autostart int default 0, iso int default 0, state text default 'creating', error text default '', created real);
create table if not exists images(id integer primary key, key text unique, name text, kind text, url text, file text, size int default 0, got int default 0, state text default 'new', error text default '');
create table if not exists nodes(id integer primary key, name text unique, url text, token text, cert text);
create table if not exists settings(k text primary key, v text);
create table if not exists events(id integer primary key, ts real, who text, msg text);
"""

_tl = threading.local()


def conn():
    c = getattr(_tl, "c", None)
    if c is None:
        c = sqlite3.connect(str(DATA / "xstem.db"), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")
        _tl.c = c
    return c


def q(sql, *a):
    return [dict(r) for r in conn().execute(sql, a).fetchall()]


def q1(sql, *a):
    r = conn().execute(sql, a).fetchone()
    return dict(r) if r else None


def x(sql, *a):
    return conn().execute(sql, a).lastrowid


conn().executescript(SCHEMA)

DEFAULTS = {"brand": "xstem", "accent": "#7aa2ff", "mode": "dark", "bg_type": "none", "bg_url": "", "dim": "0.6", "css": ""}


def setting(k, d=""):
    r = q1("select v from settings where k=?", k)
    return r["v"] if r else DEFAULTS.get(k, d)


def put(k, v):
    x("insert into settings(k,v) values(?,?) on conflict(k) do update set v=excluded.v", k, str(v))


def node_token():
    t = setting("node_token")
    if not t:
        t = "xn_" + secrets.token_urlsafe(24)
        put("node_token", t)
    return t


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "s1$%s$%s" % (salt.hex(), h.hex())


def check_pw(pw, stored):
    try:
        _, s, h = stored.split("$")
        c = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(s), n=2 ** 14, r=8, p=1, dklen=32)
        return secrets.compare_digest(c.hex(), h)
    except Exception:
        return False


def pam_ok(user, pw):
    try:
        import pam
        return bool(pam.pam().authenticate(user, pw, service="xstem"))
    except Exception:
        return False


def log(who, msg):
    x("insert into events(ts,who,msg) values(?,?,?)", time.time(), who, msg)
    x("delete from events where id < (select max(id) from events) - 1000")
