import glob
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from . import core, virt
from .core import DATA, HOME, LXC, VENDOR, log

STATE = {"state": "starting", "step": "Starting", "pct": 0, "log": [], "error": ""}
LOCK = threading.Lock()
CLI = False

PACKAGES = ["qemu-system-x86", "qemu-utils", "lxc", "genisoimage", "iptables", "iproute2", "dnsmasq-base",
            "gnupg", "wget", "xz-utils", "rsync", "openssl", "curl", "ca-certificates", "e2fsprogs", "util-linux",
            "kmod", "debootstrap", "uidmap", "psmisc"]
NEEDED_BINS = ["qemu-system-x86_64", "qemu-img", "lxc-create", "lxc-start", "genisoimage", "iptables", "ip",
               "openssl", "mkfs.ext4", "truncate", "mount"]
VENDOR_FILES = {
    "xterm.js": "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js",
    "xterm.css": "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css",
    "xterm-addon-fit.js": "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js",
}
PREFETCH_IMAGES = ["debian-12", "ubuntu-24.04"]
PREFETCH_TEMPLATES = [("debian", "bookworm"), ("ubuntu", "noble")]


def _say(line, progress=False):
    if progress and STATE["log"] and STATE["log"][-1].startswith("> "):
        STATE["log"][-1] = line
    else:
        STATE["log"] = (STATE["log"] + [line])[-300:]
    if CLI:
        if progress:
            print("\r" + line.ljust(72), end="", flush=True)
        else:
            print(line, flush=True)


def _step(name, pct):
    STATE.update(step=name, pct=pct)
    if CLI:
        print("\n== " + name, flush=True)
    STATE["log"] = (STATE["log"] + ["== " + name])[-300:]


def needed():
    return not (DATA / "bootstrap.done").exists() or not all(shutil.which(b) for b in NEEDED_BINS)


def _apt(*args, check=True):
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="a", NEEDRESTART_SUSPEND="1")
    p = subprocess.Popen(["apt-get", "-y", "-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Options::=--force-confold", *args],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, env=env)
    for line in p.stdout:
        _say(line.rstrip()[:160])
    if p.wait() and check:
        raise RuntimeError("apt-get %s failed, see log" % args[0])


def fetch_vendor():
    for name, url in VENDOR_FILES.items():
        dest = VENDOR / name
        if dest.exists() and dest.stat().st_size > 1000:
            continue
        for _ in range(3):
            try:
                r = httpx.get(url, follow_redirects=True, timeout=60)
                r.raise_for_status()
                dest.write_bytes(r.content)
                break
            except Exception as e:
                _say("could not fetch %s: %s" % (name, e))
                time.sleep(2)


def install():
    _step("Updating package lists", 5)
    _apt("update")
    _step("Installing QEMU, LXC, lxcfs and network tools", 15)
    _apt("install", *PACKAGES)
    if not Path("/usr/share/lxc/templates/lxc-download").exists():
        _say("lxc download template missing, installing lxc-templates")
        _apt("install", "lxc-templates", check=False)
    _apt("install", "lxcfs", check=False)
    missing = [b for b in NEEDED_BINS if not shutil.which(b)]
    if missing:
        raise RuntimeError("missing after install: " + ", ".join(missing))
    _step("Preparing kernel modules and services", 60)
    for mod in ("tun", "loop", "br_netfilter", "kvm", "kvm_intel", "kvm_amd"):
        subprocess.run(["modprobe", mod], capture_output=True)
    Path("/etc/modules-load.d").mkdir(exist_ok=True)
    Path("/etc/modules-load.d/xstem.conf").write_text("tun\nloop\nbr_netfilter\n")
    for d in glob.glob("/usr/lib/*/lxc") + ["/usr/lib/lxc"]:
        try:
            Path(d, "rootfs").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    subprocess.run(["systemctl", "enable", "--now", "lxcfs"], capture_output=True)
    src = shutil.which("dnsmasq")
    if src:
        shutil.copy2(src, HOME / "xdnsmasq")
    (DATA / "bootstrap.done").write_text("ok\n")


def runtime():
    _step("Starting virtual network", 75)
    virt.ensure_network()
    _step("Fetching console assets", 80)
    fetch_vendor()
    _step("Registering images", 84)
    virt.seed_images()
    core.x("update guests set state='error',error='interrupted by restart' where state='creating'")
    virt.write_hosts()
    virt.apply_forwards()
    virt.autostart_all()


def warm_templates():
    for dist, rel in PREFETCH_TEMPLATES:
        _say("> caching container template %s/%s" % (dist, rel), progress=True)
        tmp = DATA / "mnt" / "warm"
        tmp.mkdir(parents=True, exist_ok=True)
        cmd = ["lxc-create", "-P", LXC, "-n", "xs-warm", "-B", "dir", "--dir", tmp, "-t", "download", "--", "-d", dist, "-r", rel, "-a", "amd64"]
        r = virt.sh(*cmd, check=False, timeout=1800)
        if r.returncode:
            virt.sh("lxc-destroy", "-P", LXC, "-n", "xs-warm", "-f", check=False)
            r = virt.sh(*cmd, "--no-validate", check=False, timeout=1800)
        virt.sh("lxc-destroy", "-P", LXC, "-n", "xs-warm", "-f", check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        _say("\ncontainer template %s/%s %s" % (dist, rel, "cached" if r.returncode == 0 else "failed: " + r.stderr.strip()[-120:]))


def prefetch():
    marker = DATA / "prefetch.done"
    if marker.exists():
        return
    ok = True
    virt.seed_images()
    for key in PREFETCH_IMAGES:
        im = core.q1("select * from images where key=?", key)
        if not im or im["state"] == "ready":
            continue
        t = threading.Thread(target=virt.download_image, args=(im["id"],), daemon=True)
        t.start()
        while t.is_alive():
            t.join(2)
            cur = core.q1("select * from images where id=?", im["id"])
            p = int(cur["got"] * 100 / cur["size"]) if cur["size"] else 0
            _say("> downloading %s %d%%  %d MB" % (im["name"], p, cur["got"] >> 20), progress=True)
        cur = core.q1("select * from images where id=?", im["id"])
        _say("\nimage %s %s" % (im["name"], "ready" if cur["state"] == "ready" else "failed: " + cur["error"]))
        ok = ok and cur["state"] == "ready"
    warm_templates()
    if ok:
        marker.write_text("ok\n")
        log("system", "default images and container templates cached")


def run():
    if not LOCK.acquire(blocking=False):
        return
    try:
        STATE.update(state="running", error="", log=[])
        if needed():
            install()
        runtime()
        STATE.update(state="done", step="Ready", pct=100)
        if not (DATA / "prefetch.done").exists():
            threading.Thread(target=prefetch, daemon=True).start()
    except Exception as e:
        STATE.update(state="error", error=str(e))
        _say("ERROR: %s" % e)
    finally:
        LOCK.release()


def start():
    threading.Thread(target=run, daemon=True).start()


def setup(skip_images=False):
    global CLI
    CLI = True
    install()
    runtime()
    if not skip_images:
        _step("Downloading default images (Debian 12, Ubuntu 24.04) and container templates", 88)
        prefetch()
    print("\n== setup complete", flush=True)
