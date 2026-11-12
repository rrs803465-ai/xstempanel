# xstem panel

Open-source VM and container panel: QEMU/KVM machines, LXC containers, live stats, web console, panel users, API keys, multi-node.

## Install (one command)

    curl -fsSL https://raw.githubusercontent.com/rrs803465-ai/xstempanel/main/install.sh | sudo bash

`install.sh` is self-contained: the whole panel is embedded in it. It installs Python, QEMU, LXC, lxcfs and the network tools with apt, builds the virtual network, downloads the console assets, Debian 12 and Ubuntu 24.04 cloud images and the Debian/Ubuntu LXC templates, then starts the panel. Open `https://SERVER_IP:8443`, realm **Linux PAM**, user `root`.

Run the same command again to update. Data stays in `/var/lib/xstem`, config in `/etc/xstem`. `XSTEM_SKIP_IMAGES=1` skips the big downloads (the panel fetches them from the Images page).

## Disks
- Containers get a dedicated ext4 disk of the size you choose (loop file), so `df -h` and screenfetch inside show that size. lxcfs makes `free`, `nproc` and `uptime` show the container's own limits.
- VMs get a qcow2 disk of the chosen size, grown automatically on first boot.

## Editing the panel
Edit files under `xstem/`, then run `python3 build.py` to regenerate the single-file `install.sh`. Only `install.sh` needs to live in your GitHub repo.
