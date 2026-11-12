import os
import sys


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    from . import core
    if cmd == "token":
        print(core.node_token())
        return
    if cmd == "setup":
        from . import bootstrap
        bootstrap.setup(skip_images=os.environ.get("XSTEM_SKIP_IMAGES") == "1")
        return
    import uvicorn
    cert, key = core.ETC / "cert.pem", core.ETC / "key.pem"
    kw = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)} if cert.exists() and key.exists() else {}
    uvicorn.run("xstem.main:app", host=os.environ.get("XSTEM_HOST", "0.0.0.0"), port=int(os.environ.get("XSTEM_PORT", "8443")), log_level="warning", **kw)


if __name__ == "__main__":
    main()
