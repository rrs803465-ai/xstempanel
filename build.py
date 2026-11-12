#!/usr/bin/env python3
"""Builds the single-file installer: python3 build.py  ->  install.sh"""
import base64, io, pathlib, tarfile, textwrap

root = pathlib.Path(__file__).parent
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as t:
    def keep(ti):
        n = ti.name
        if "__pycache__" in n or n.endswith(".pyc") or "/vendor/" in n:
            return None
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti
    t.add(root / "xstem", arcname="xstem", filter=keep)
    t.add(root / "requirements.txt", arcname="requirements.txt", filter=keep)
payload = "\n".join(textwrap.wrap(base64.b64encode(buf.getvalue()).decode(), 100))
out = (root / "installer.sh.in").read_text().replace("@@PAYLOAD@@", payload)
(root / "install.sh").write_text(out)
(root / "install.sh").chmod(0o755)
print("install.sh", len(out) // 1024, "KB")
