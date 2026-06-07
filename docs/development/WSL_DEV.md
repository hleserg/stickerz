# Developing on WSL + CUDA

If a project built from this template grows **GPU/ML stages** (diffusion, training,
ONNX inference, a local generation UI), the recommended development environment is
**WSL2 (Ubuntu) with an NVIDIA CUDA driver**, where models cache locally and a
backend is reachable to validate output. Linux also removes Windows-specific
friction (non-ASCII paths breaking image I/O, missing `jq`/`make`, non-portable
shell hooks).

## One-time setup

```bash
# In WSL (Ubuntu), with the NVIDIA driver installed on Windows and `nvidia-smi` working:
git clone <your repo>
cd <your repo>

curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv if absent
uv sync --all-extras                              # create .venv, install light/CPU deps
uv run pre-commit install                         # repo hooks
make check                                         # DoD gate — must be green
```

`make` and `jq` exist on Linux, so the `Makefile` targets and any shell hooks work
directly (unlike the Windows host).

## Split CPU vs GPU dependencies

Keep the light/CPU deps in `[project.dependencies]` (install with `uv sync
--all-extras`) and put the heavy stack (`torch`, `torchvision`, `onnxruntime`, …)
in a **PEP-735 dependency group**, installed only on the GPU box:

```bash
uv sync --group gpu     # pulls CUDA torch from the cuXXX index (see pyproject.toml)
nvidia-smi              # confirm the GPU is visible from WSL
```

`uv sync --all-extras` (CI, fresh clone) deliberately does **not** install the
`gpu` group, so the CPU path and `make check` stay fast and `pip-audit` never scans
the heavy tree. The group is still resolved and pinned in `uv.lock` on every
platform. Keep heavy backends behind a `Protocol` and import them lazily so unit
tests and `make check` run with **no GPU**.

## Exposing a local UI / service to the LAN (WSL networking)

> **THE RULE: a WSL service must listen on `0.0.0.0` inside WSL, never `127.0.0.1`.**

Windows reaches a WSL service through the WSL distro's **eth0 IP** (e.g.
`172.22.x.x`), via a `netsh portproxy`. A service bound to WSL **loopback**
(`127.0.0.1`) is invisible to that proxy and unreachable from the host or the LAN.
So bind servers to `0.0.0.0` (expose a `*_HOST` setting that defaults to `0.0.0.0`,
or override it, e.g. `APP_UI_HOST=0.0.0.0`).

**Reaching it from the Windows host / another machine on the LAN (Windows 10):**

> Note: WSL **mirrored networking** (`networkingMode=mirrored`, which shares
> `localhost` host↔WSL and needs no portproxy) requires **Windows 11 22H2+**. On
> Windows 10 that `.wslconfig` line is silently ignored and WSL falls back to NAT —
> so you need a host-side portproxy + firewall rule.

```powershell
# Elevated PowerShell. Re-derive the WSL IP (it changes when WSL restarts):
$ip = (wsl hostname -I).Trim().Split(' ')[0]; "WSL IP = $ip"
$port = 7860
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$port 2>$null
netsh interface portproxy add    v4tov4 listenaddress=0.0.0.0 listenport=$port connectaddress=$ip connectport=$port
# LAN-scoped firewall rule (all the convenience, none of the public-network exposure):
netsh advfirewall firewall delete rule name=WSL_LAN_$port 2>$null
netsh advfirewall firewall add    rule name=WSL_LAN_$port dir=in action=allow protocol=TCP localport=$port remoteip=LocalSubnet
```

Then reach it at `http://<windows-LAN-IP>:<port>` from any machine on the subnet
(or `localhost:<port>` on the host itself). To make it survive reboots/WSL
restarts, drive that script from a Scheduled Task on logon + a short interval —
`schtasks /Create` is more robust than `Register-ScheduledTask` on localized /
non-domain Windows (the latter can fail to map the user name to a SID). Prefer a
LAN-scoped (`remoteip=LocalSubnet`) firewall rule over disabling the firewall.

## Keep the code OS-agnostic

The project should keep running on Windows too:

- **Never call image/file I/O with a raw path that may be non-ASCII** — prefer
  Pillow (`Image.open`) or decode via `np.fromfile` + `cv2.imdecode` if OpenCV is used.
- Prefer pure stdlib path handling; don't hardcode shell tools in committed code.

## Local hooks in WSL

The committed `.claude/settings.json` carries the shared command allowlist + hooks
(format-on-edit, a pre-PR `make check` gate, a SessionStart pre-commit install +
skill-authoring policy). Personal/machine-specific overrides (autonomous
`acceptEdits`, push/PR permissions) go in `.claude/settings.local.json`
(git-ignored) — run `/update-config` to manage them.
