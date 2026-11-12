import asyncio
import fcntl
import json
import os
import pty
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path

import httpx
import psutil

from .core import (BRIDGE, DATA, DISKS, ETC, HOME, IMAGES, LXC, RUN, SUBNET, log, q, q1, x)

DNSMASQ = HOME / "xdnsmasq"

CATALOG = [
    {"key": "debian-13", "name": "Debian 13", "kind": "cloud", "url": "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"},
    {"key": "debian-12", "name": "Debian 12", "kind": "cloud", "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"},
    {"key": "ubuntu-24.04", "name": "Ubuntu 24.04", "kind": "cloud", "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"},
    {"key": "ubuntu-22.04", "name": "Ubuntu 22.04", "kind": "cloud", "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"},
    {"key": "rocky-9", "name": "Rocky Linux 9", "kind": "cloud", "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2"},
    {"key": "alma-9", "name": "AlmaLinux 9", "kind": "cloud", "url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2"},
]
CT_TEMPLATES = ["debian/bookworm", "debian/trixie", "ubuntu/noble", "ubuntu/jammy", "alpine/edge", "rockylinux/9", "almalinux/9", "archlinux/current"]


def sh(*cmd, check=True, input=None, timeout=None):
    r = subprocess.run([str(c) for c in cmd], input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout or "%s failed" % cmd[0]).strip()[-700:])
    return r


def _pid(path):
    try:
        pid = int(Path(path).read_text().strip())
        return pid if psutil.pid_exists(pid) else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- network

def next_ip():
    used = {g["ip"] for g in q("select ip from guests")}
    for i in range(10, 251):
        if "%s.%d" % (SUBNET, i) not in used:
            return "%s.%d" % (SUBNET, i)
    raise RuntimeError("address pool exhausted")


def new_mac():
    used = {g["mac"] for g in q("select mac from guests")}
    while True:
        m = "52:54:00:%02x:%02x:%02x" % tuple(secrets.token_bytes(3))
        if m not in used:
            return m


def free_port():
    used = set()
    for g in q("select forwards from guests"):
        used |= {f[0] for f in json.loads(g["forwards"])}
    for p in range(20000, 30000):
        if p in used:
            continue
        s = socket.socket()
        try:
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free host ports")


def write_hosts(reload=True):
    lines = ["%s,%s,%s" % (g["mac"], g["ip"], g["name"]) for g in q("select mac,ip,name from guests")]
    f = ETC / "dhcp-hosts"
    f.write_text("\n".join(lines) + ("\n" if lines else ""))
    f.chmod(0o644)
    pid = _pid(RUN / "dnsmasq.pid")
    if reload and pid:
        os.kill(pid, signal.SIGHUP)


def apply_forwards():
    sh("iptables", "-t", "nat", "-N", "XSTEM", check=False)
    for chain in ("PREROUTING", "OUTPUT"):
        rule = [chain, "-m", "addrtype", "--dst-type", "LOCAL", "-j", "XSTEM"]
        if sh("iptables", "-t", "nat", "-C", *rule, check=False).returncode:
            sh("iptables", "-t", "nat", "-A", *rule)
    sh("iptables", "-t", "nat", "-F", "XSTEM")
    for g in q("select ip,forwards from guests"):
        for hp, gp in json.loads(g["forwards"]):
            sh("iptables", "-t", "nat", "-A", "XSTEM", "-p", "tcp", "--dport", hp, "-j", "DNAT", "--to-destination", "%s:%s" % (g["ip"], gp), check=False)


def ensure_network():
    if not Path("/sys/class/net/%s" % BRIDGE).exists():
        sh("ip", "link", "add", "name", BRIDGE, "type", "bridge")
    if "%s.1/24" % SUBNET not in sh("ip", "-4", "addr", "show", "dev", BRIDGE).stdout:
        sh("ip", "addr", "add", "%s.1/24" % SUBNET, "dev", BRIDGE)
    sh("ip", "link", "set", BRIDGE, "up")
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
    rules = [
        (["-t", "nat"], "POSTROUTING", ["-s", "%s.0/24" % SUBNET, "!", "-d", "%s.0/24" % SUBNET, "-j", "MASQUERADE"], "-A"),
        ([], "FORWARD", ["-i", BRIDGE, "-j", "ACCEPT"], "-I"),
        ([], "FORWARD", ["-o", BRIDGE, "-j", "ACCEPT"], "-I"),
        ([], "INPUT", ["-i", BRIDGE, "-j", "ACCEPT"], "-I"),
    ]
    for tbl, chain, rule, op in rules:
        if sh("iptables", *tbl, "-C", chain, *rule, check=False).returncode:
            sh("iptables", *tbl, op, chain, *rule)
    write_hosts(reload=False)
    conf = ETC / "dnsmasq.conf"
    conf.write_text(
        "port=0\ninterface=%s\nbind-interfaces\nexcept-interface=lo\n"
        "dhcp-range=%s.0,static,255.255.255.0,12h\ndhcp-hostsfile=%s/dhcp-hosts\n"
        "dhcp-leasefile=%s/dnsmasq.leases\ndhcp-option=3,%s.1\ndhcp-option=6,1.1.1.1,8.8.8.8\npid-file=%s/dnsmasq.pid\n"
        % (BRIDGE, SUBNET, ETC, DATA, SUBNET, RUN))
    conf.chmod(0o644)
    pid = _pid(RUN / "dnsmasq.pid")
    if pid:
        os.kill(pid, signal.SIGHUP)
    else:
        binary = DNSMASQ if DNSMASQ.exists() else shutil.which("dnsmasq")
        sh(binary, "--conf-file=%s" % conf)


# ---------------------------------------------------------------- images

def seed_images():
    for c in CATALOG:
        if not q1("select id from images where key=?", c["key"]):
            x("insert into images(key,name,kind,url,file) values(?,?,?,?,?)", c["key"], c["name"], c["kind"], c["url"], c["key"] + ".img")
    x("update images set state='new' where state='downloading'")
    for im in q("select * from images where state='ready'"):
        if not (IMAGES / im["file"]).exists():
            x("update images set state='new',got=0 where id=?", im["id"])


def download_image(iid):
    im = q1("select * from images where id=?", iid)
    x("update images set state='downloading',got=0,size=0,error='' where id=?", iid)
    part = IMAGES / (im["file"] + ".part")
    try:
        with httpx.stream("GET", im["url"], follow_redirects=True, timeout=30) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            got, tick = 0, time.time()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if time.time() - tick > 1.5:
                        x("update images set got=?,size=? where id=?", got, total, iid)
                        tick = time.time()
        if got < (1 << 20):
            raise RuntimeError("download too small, check the URL")
        if im["kind"] == "cloud":
            sh("qemu-img", "info", "--output=json", part)
        os.replace(part, IMAGES / im["file"])
        x("update images set state='ready',got=?,size=? where id=?", got, got, iid)
        log("system", "image ready: %s" % im["name"])
    except Exception as e:
        part.unlink(missing_ok=True)
        x("update images set state='error',error=? where id=?", str(e)[:300], iid)


def start_download(iid):
    threading.Thread(target=download_image, args=(iid,), daemon=True).start()


# ---------------------------------------------------------------- guests: status

def vm_pid(g):
    pid = _pid(RUN / ("vm%d.pid" % g["id"]))
    try:
        return pid if pid and psutil.Process(pid).name().startswith("qemu") else 0
    except psutil.Error:
        return 0


_ctp = {}


def ct_pid(name):
    c = _ctp.get(name)
    if c and ((c[1] and psutil.pid_exists(c[1])) or (not c[1] and time.time() - c[0] < 3)):
        return c[1]
    r = sh("lxc-info", "-P", LXC, "-n", name, "-p", "-H", check=False)
    try:
        pid = max(int(r.stdout.strip().split()[0]), 0)
    except (ValueError, IndexError):
        pid = 0
    _ctp[name] = (time.time(), pid)
    return pid


def status(g):
    if g["state"] != "ready":
        return g["state"]
    return "running" if (vm_pid(g) if g["kind"] == "vm" else ct_pid(g["name"])) else "stopped"


def _rd(path):
    try:
        return int(Path(path).read_text().split()[0])
    except Exception:
        return 0


def _cgroup(pid):
    for line in Path("/proc/%d/cgroup" % pid).read_text().splitlines():
        if line.startswith("0::"):
            parts = [s for s in line[3:].split("/") if s]
            for i, s in enumerate(parts):
                if s.startswith("lxc.payload."):
                    parts = parts[:i + 1]
                    break
            return Path("/sys/fs/cgroup").joinpath(*parts)
    raise RuntimeError("no cgroup")


_prev = {}


def sample(g, st):
    z = {"running": st == "running", "cpu": 0.0, "mem_used": 0, "mem_max": g["ram"] * 1048576, "disk_used": 0,
         "disk_max": g["disk"] * 2 ** 30, "net_in": 0, "net_out": 0, "uptime": 0}
    if st != "running":
        _prev.pop(g["id"], None)
        return z
    try:
        now = time.time()
        if g["kind"] == "vm":
            p = psutil.Process(vm_pid(g))
            t = p.cpu_times()
            sec = t.user + t.system
            z["mem_used"] = p.memory_info().rss
            z["uptime"] = int(now - p.create_time())
            z["disk_used"] = os.stat(DISKS / str(g["id"]) / "disk.qcow2").st_blocks * 512
            ifn = "xt%d" % g["id"]
        else:
            pid = ct_pid(g["name"])
            cg = _cgroup(pid)
            sec = 0.0
            for line in (cg / "cpu.stat").read_text().splitlines():
                if line.startswith("usage_usec"):
                    sec = int(line.split()[1]) / 1e6
            z["mem_used"] = _rd(cg / "memory.current")
            try:
                sv = os.statvfs("/proc/%d/root/" % pid)
                z["disk_max"] = sv.f_blocks * sv.f_frsize
                z["disk_used"] = (sv.f_blocks - sv.f_bfree) * sv.f_frsize
            except OSError:
                pass
            z["uptime"] = int(now - psutil.Process(pid).create_time())
            ifn = "xv%d" % g["id"]
        pr = _prev.get(g["id"])
        cpu = pr[2] if pr else 0.0
        if pr is None or now - pr[0] >= 1:
            if pr:
                cpu = max(0.0, min(100.0, (sec - pr[1]) / (now - pr[0]) / g["cores"] * 100))
            _prev[g["id"]] = (now, sec, cpu)
        z["cpu"] = round(cpu, 1)
        z["net_in"] = _rd("/sys/class/net/%s/statistics/tx_bytes" % ifn)
        z["net_out"] = _rd("/sys/class/net/%s/statistics/rx_bytes" % ifn)
    except Exception:
        pass
    return z


# ---------------------------------------------------------------- guests: VM

def qmp(g, cmd):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    try:
        s.connect(str(RUN / ("vm%d.qmp" % g["id"])))
        f = s.makefile("rw")
        f.readline()
        for c in ("qmp_capabilities", cmd):
            f.write(json.dumps({"execute": c}) + "\n")
            f.flush()
            while True:
                line = f.readline()
                if not line:
                    break
                d = json.loads(line)
                if "return" in d or "error" in d:
                    break
    finally:
        s.close()


def vm_cleanup(g):
    sh("ip", "link", "del", "xt%d" % g["id"], check=False)
    for ext in ("pid", "qmp", "ser"):
        (RUN / ("vm%d.%s" % (g["id"], ext))).unlink(missing_ok=True)


def vm_start(g):
    if vm_pid(g):
        return
    d = DISKS / str(g["id"])
    vm_cleanup(g)
    tap = "xt%d" % g["id"]
    sh("ip", "tuntap", "add", "dev", tap, "mode", "tap")
    sh("ip", "link", "set", tap, "master", BRIDGE)
    sh("ip", "link", "set", tap, "up")
    kvm = os.path.exists("/dev/kvm")
    a = ["qemu-system-x86_64", "-name", "xs-%d" % g["id"], "-machine", "q35,accel=%s" % ("kvm" if kvm else "tcg"),
         "-cpu", "host" if kvm else "max", "-smp", g["cores"], "-m", g["ram"],
         "-drive", "file=%s,if=virtio,format=qcow2,cache=writeback" % (d / "disk.qcow2")]
    if (d / "seed.iso").exists():
        a += ["-drive", "file=%s,if=virtio,format=raw,readonly=on" % (d / "seed.iso")]
    im = q1("select file from images where key=?", g["image"])
    if g["iso"] and im and (IMAGES / im["file"]).exists():
        a += ["-cdrom", IMAGES / im["file"], "-boot", "order=c,once=d"]
    a += ["-netdev", "tap,id=n0,ifname=%s,script=no,downscript=no" % tap,
          "-device", "virtio-net-pci,netdev=n0,mac=%s" % g["mac"], "-device", "virtio-rng-pci", "-display", "none",
          "-chardev", "socket,id=ser0,path=%s,server=on,wait=off" % (RUN / ("vm%d.ser" % g["id"])), "-serial", "chardev:ser0",
          "-chardev", "socket,id=qmp0,path=%s,server=on,wait=off" % (RUN / ("vm%d.qmp" % g["id"])), "-mon", "chardev=qmp0,mode=control",
          "-pidfile", RUN / ("vm%d.pid" % g["id"]), "-daemonize"]
    try:
        sh(*a, timeout=60)
    except Exception:
        vm_cleanup(g)
        raise


def vm_stop(g):
    pid = vm_pid(g)
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not psutil.pid_exists(pid):
                break
            time.sleep(0.2)
        else:
            os.kill(pid, signal.SIGKILL)
    vm_cleanup(g)


def _vm_reboot(g):
    qmp(g, "system_powerdown")
    for _ in range(90):
        if not vm_pid(g):
            break
        time.sleep(1)
    else:
        vm_stop(g)
    vm_start(g)


# ---------------------------------------------------------------- guests: LXC

def ct_start(g):
    _ctp.pop(g["name"], None)
    d = LXC / g["name"]
    try:
        sh("lxc-start", "-P", LXC, "-n", g["name"], "-d", "-o", d / "start.log", "-l", "ERROR", timeout=60)
    except RuntimeError as e:
        tail = ""
        try:
            tail = (d / "start.log").read_text()[-400:]
        except Exception:
            pass
        raise RuntimeError(("%s %s" % (e, tail)).strip())


def ct_stop(g, force=False):
    _ctp.pop(g["name"], None)
    sh("lxc-stop", "-P", LXC, "-n", g["name"], *(["-k"] if force else ["-t", "30"]), check=False, timeout=90)
    _ctp.pop(g["name"], None)


def _bg(fn, *a):
    def run():
        try:
            fn(*a)
        except Exception as e:
            log("system", "background action failed: %s" % e)
    threading.Thread(target=run, daemon=True).start()


def power(g, action):
    vm = g["kind"] == "vm"
    running = vm_pid(g) if vm else ct_pid(g["name"])
    if action == "start":
        if running:
            raise RuntimeError("already running")
        return vm_start(g) if vm else ct_start(g)
    if not running:
        raise RuntimeError("machine is not running")
    if action == "stop":
        return vm_stop(g) if vm else ct_stop(g, True)
    if action == "shutdown":
        return qmp(g, "system_powerdown") if vm else _bg(ct_stop, g)
    if action == "reboot":
        return _bg(_vm_reboot, g) if vm else _bg(lambda: sh("lxc-stop", "-P", LXC, "-n", g["name"], "-r", "-t", "30", timeout=90))
    raise RuntimeError("unknown action")


# ---------------------------------------------------------------- guests: provisioning

def _hash(pw):
    return sh("openssl", "passwd", "-6", "-stdin", input=pw + "\n").stdout.strip()


def _seed(d, g, pw):
    ud = ("#cloud-config\nhostname: %s\nmanage_etc_hosts: true\nssh_pwauth: true\ndisable_root: false\n"
          "chpasswd:\n  expire: false\n  list:\n    - %s\n"
          "write_files:\n  - path: /etc/ssh/sshd_config.d/01-xstem.conf\n    content: |\n      PermitRootLogin yes\n      PasswordAuthentication yes\n"
          "runcmd:\n  - [ sh, -c, \"systemctl restart ssh || systemctl restart sshd || true\" ]\n") % (g["name"], json.dumps("root:" + _hash(pw)))
    (d / "user-data").write_text(ud)
    (d / "meta-data").write_text("instance-id: xs-%d\nlocal-hostname: %s\n" % (g["id"], g["name"]))
    sh("genisoimage", "-output", d / "seed.iso", "-volid", "cidata", "-joliet", "-rock", d / "user-data", d / "meta-data")


def _provision_vm(g, pw):
    im = q1("select * from images where key=?", g["image"])
    if not im or im["state"] != "ready":
        raise RuntimeError("image is not downloaded yet")
    base, d = IMAGES / im["file"], DISKS / str(g["id"])
    d.mkdir(parents=True, exist_ok=True)
    disk = d / "disk.qcow2"
    if im["kind"] == "iso":
        sh("qemu-img", "create", "-f", "qcow2", disk, "%dG" % g["disk"])
        return
    info = json.loads(sh("qemu-img", "info", "--output=json", base).stdout)
    gb = max(g["disk"], -(-info["virtual-size"] // 2 ** 30))
    sh("qemu-img", "create", "-f", "qcow2", "-b", base, "-F", info["format"], disk, "%dG" % gb)
    _seed(d, g, pw)


def ct_img(g):
    return DATA / "ctdisks" / ("%s.img" % g["name"])


def _provision_ct(g, pw):
    dist, rel = g["image"].split("/", 1)
    img = ct_img(g)
    mnt = DATA / "mnt" / ("ct%d" % g["id"])
    img.parent.mkdir(parents=True, exist_ok=True)
    mnt.mkdir(parents=True, exist_ok=True)
    sh("truncate", "-s", "%dG" % g["disk"], img)
    sh("mkfs.ext4", "-F", "-q", "-m", "1", "-E", "lazy_itable_init=1,lazy_journal_init=1,nodiscard", "-L", "xs%d" % g["id"], img)
    sh("mount", "-o", "loop", img, mnt)
    try:
        try:
            (mnt / "lost+found").rmdir()
        except OSError:
            pass
        cmd = ["lxc-create", "-P", LXC, "-n", g["name"], "-B", "dir", "--dir", mnt, "-t", "download", "--", "-d", dist, "-r", rel, "-a", "amd64"]
        try:
            sh(*cmd, timeout=1800)
        except RuntimeError:
            sh("lxc-destroy", "-P", LXC, "-n", g["name"], "-f", check=False)
            sh(*cmd, "--no-validate", timeout=1800)
        cfg = LXC / g["name"] / "config"
        keep = [l for l in cfg.read_text().splitlines() if not l.startswith(("lxc.net.", "lxc.uts.name", "lxc.rootfs.path"))]
        keep += ["lxc.rootfs.path = loop:%s" % img, "lxc.uts.name = %s" % g["name"], "lxc.net.0.type = veth", "lxc.net.0.link = %s" % BRIDGE,
                 "lxc.net.0.flags = up", "lxc.net.0.name = eth0", "lxc.net.0.hwaddr = %s" % g["mac"], "lxc.net.0.veth.pair = xv%d" % g["id"],
                 "lxc.cgroup2.memory.max = %d" % (g["ram"] * 1048576), "lxc.cgroup2.cpu.max = %d 100000" % (g["cores"] * 100000)]
        cfg.write_text("\n".join(keep) + "\n")
        shadow = mnt / "etc/shadow"
        if shadow.exists():
            lines = shadow.read_text().splitlines()
            for i, l in enumerate(lines):
                if l.startswith("root:"):
                    f = l.split(":")
                    f[1], f[2] = _hash(pw), str(int(time.time() // 86400))
                    lines[i] = ":".join(f)
            shadow.write_text("\n".join(lines) + "\n")
        conf_d = mnt / "etc/ssh/sshd_config.d"
        if conf_d.is_dir():
            (conf_d / "01-xstem.conf").write_text("PermitRootLogin yes\nPasswordAuthentication yes\n")
        elif (mnt / "etc/ssh/sshd_config").exists():
            with open(mnt / "etc/ssh/sshd_config", "a") as f:
                f.write("\nPermitRootLogin yes\nPasswordAuthentication yes\n")
    finally:
        sh("sync", check=False)
        sh("umount", mnt, check=False)
        shutil.rmtree(mnt, ignore_errors=True)


def provision(gid, pw):
    g = q1("select * from guests where id=?", gid)
    try:
        write_hosts()
        apply_forwards()
        (_provision_vm if g["kind"] == "vm" else _provision_ct)(g, pw)
        x("update guests set state='ready' where id=?", gid)
        log("system", "%s %s is ready" % (g["kind"], g["name"]))
        if g["autostart"]:
            power(g, "start")
    except Exception as e:
        x("update guests set state='error',error=? where id=?", str(e)[:500], gid)
        log("system", "provision of %s failed: %s" % (g["name"], str(e)[:200]))


def destroy(g):
    if g["state"] == "ready":
        try:
            power(g, "stop")
        except RuntimeError:
            pass
    if g["kind"] == "vm":
        vm_cleanup(g)
        shutil.rmtree(DISKS / str(g["id"]), ignore_errors=True)
    else:
        sh("lxc-destroy", "-P", LXC, "-n", g["name"], "-f", check=False, timeout=120)
        shutil.rmtree(LXC / g["name"], ignore_errors=True)
        ct_img(g).unlink(missing_ok=True)
    x("delete from guests where id=?", g["id"])
    write_hosts()
    apply_forwards()


def autostart_all():
    for g in q("select * from guests where autostart=1 and state='ready'"):
        try:
            power(g, "start")
        except Exception as e:
            log("system", "autostart %s failed: %s" % (g["name"], e))


# ---------------------------------------------------------------- console

class SockConsole:
    def __init__(self, r, w):
        self.r, self.w = r, w

    async def read(self):
        return await self.r.read(4096)

    def write(self, b):
        self.w.write(b)

    def resize(self, c, r):
        pass

    def close(self):
        try:
            self.w.close()
        except Exception:
            pass


class PtyConsole:
    def __init__(self, name):
        master, slave = pty.openpty()

        def ctty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.p = subprocess.Popen(
            ["lxc-attach", "-P", str(LXC), "-n", name, "-v", "TERM=xterm-256color", "-v", "HOME=/root", "--", "/bin/sh", "-c",
             "command -v bash >/dev/null && exec bash -l; exec sh -l"],
            stdin=slave, stdout=slave, stderr=slave, preexec_fn=ctty, close_fds=True)
        os.close(slave)
        self.fd, self.loop, self.queue = master, asyncio.get_running_loop(), asyncio.Queue()
        self.loop.add_reader(master, self._on_data)

    def _on_data(self):
        try:
            d = os.read(self.fd, 4096)
        except OSError:
            d = b""
        if not d:
            self.loop.remove_reader(self.fd)
        self.queue.put_nowait(d)

    async def read(self):
        return await self.queue.get()

    def write(self, b):
        os.write(self.fd, b)

    def resize(self, c, r):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(r), int(c), 0, 0))

    def close(self):
        try:
            self.loop.remove_reader(self.fd)
            os.close(self.fd)
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


async def open_console(g):
    if g["state"] != "ready":
        raise RuntimeError("machine is not ready")
    if g["kind"] == "vm":
        if not vm_pid(g):
            raise RuntimeError("machine is not running")
        r, w = await asyncio.open_unix_connection(str(RUN / ("vm%d.ser" % g["id"])))
        w.write(b"\r")
        return SockConsole(r, w)
    if not ct_pid(g["name"]):
        raise RuntimeError("container is not running")
    return PtyConsole(g["name"])
