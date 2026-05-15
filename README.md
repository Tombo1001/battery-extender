# Battery Extender TUI

A terminal-based power management dashboard for Windows 11 laptops with degraded batteries.
Shows real-time power draw, top energy-consuming processes, session energy trends, and lets you
tune brightness, CPU limits, and power plans — all from one screen.

[![Security Audit](https://github.com/Tombo1001/battery-extender/actions/workflows/security-audit.yml/badge.svg)](https://github.com/Tombo1001/battery-extender/actions/workflows/security-audit.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What it does

| Panel | Description |
|---|---|
| **Top bar** | Live battery %, health vs design capacity, real-time watts (from WMI), estimated runtime |
| **Power Profile** | Switch between your installed Windows power plans |
| **CPU Limit** | Cap maximum CPU speed on battery (e.g. 60% = big power saving with mild performance impact). RAG bar shows where you are |
| **Brightness** | Adjust screen brightness in ±5/±10% steps. RAG bar shows current level |
| **System Status** | Remaining/full/design capacity in mWh, draw in watts, estimated runtime, RAM |
| **Current Snapshot** | Top processes right now by CPU% with estimated watt contribution |
| **Session Energy** | Cumulative mWh consumed per process since launch, with trend arrows (↑ ↓ →) |

The included file [`battery-report.html`](battery-report.html) is an anonymised example report
from a Dell Inspiron 7400 whose battery has degraded to ~37% of its original capacity
(18,992 mWh remaining from a 51,999 mWh design). It shows the kind of data Windows records
and is included so you can see what to look for in your own report.

To generate your own battery report (do not commit it — it contains your computer name):

```
powercfg /batteryreport /output battery-report-mine.html
```

Then open the HTML file in any browser.

---

## Requirements

- **Windows 11** (or Windows 10 with WMI support)
- **Python 3.10 or newer** — [download from python.org](https://www.python.org/downloads/)
- **Administrator privileges** for power plan switching, brightness control, and CPU throttling
  (read-only stats mode works without admin)

---

## Installation — step by step

### Step 1 — Install Python

Go to [python.org/downloads](https://www.python.org/downloads/) and download the latest
Python 3.x installer for Windows. Run it and **tick "Add Python to PATH"** before clicking
Install.

To check it worked, open a Command Prompt and type:

```
python --version
```

You should see something like `Python 3.13.0`.

### Step 2 — Download this project

**Option A — Git (if you have it installed):**

```
git clone https://github.com/Tombo1001/battery-extender.git
cd battery-extender
```

**Option B — Download ZIP:**

Click the green **Code** button on GitHub → **Download ZIP**. Extract the folder somewhere
convenient (e.g. `C:\Tools\battery-extender`).

Then open a Command Prompt, type `cd ` and drag the extracted folder onto the window, then
press Enter.

### Step 3 — Install dependencies

Inside the project folder, run:

```
pip install -r requirements.txt
```

This installs the two libraries the script needs (`textual` for the TUI and `psutil` for
system metrics). All versions are pinned — see [Supply Chain Security](#supply-chain-security).

### Step 4 — Run the tool

**For full functionality (recommended) — run as Administrator:**

Right-click **Command Prompt** or **PowerShell** in the Start menu and choose
**"Run as administrator"**. Then navigate to the project folder and run:

```
python battery_tui.py
```

**Read-only mode (no admin needed):**

```
python battery_tui.py
```

Without admin you can still see all the stats and the process tables — you just cannot
change power plans, brightness, or CPU limits.

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `q` | Quit |
| `r` | Force refresh |
| `b` | Brightness +10% |
| `B` (Shift+b) | Brightness −10% |
| `s` | Sleep (2 second delay) |
| `h` | Hibernate (2 second delay) |
| `Tab` | Move focus between panels |
| `↑` / `↓` | Navigate lists |
| `Enter` | Confirm selection |

---

## Understanding the RAG bars

The CPU Limit and Brightness controls each show a colour-coded bar:

```
█████████████░░░░░░░░░  60%
green  amber    red
```

- **Green zone (left):** lower value — better for battery life
- **Amber zone (middle):** moderate
- **Red zone (right):** high — most power consumption
- **`▼` marker:** your current setting

Both controls follow the same rule: further left = longer battery life.

---

## Session Energy panel

The right half of the bottom section accumulates energy estimates across the whole
session. Processes are **aggregated by name** — so if Windows runs twenty `svchost.exe`
instances, their combined cost appears as one row. This gives an honest picture of what
is actually draining your battery over time.

**Trend arrows:**

| Arrow | Meaning |
|---|---|
| `↑` | Process is consuming more CPU/power recently than it was earlier |
| `→` | Stable — usage is roughly constant |
| `↓` | Process is consuming less power than earlier in the session |
| `…` | Not enough data yet (needs ~24 seconds) |

The `svchost.exe` entry is almost always large — this is normal. It hosts many background
Windows services. Watch for it spiking with `↑` over time, which can indicate Windows
Update, Defender scans, or telemetry activity.

---

## Supply chain security

All dependency versions are **pinned exactly** in `requirements.txt`, including transitive
(indirect) dependencies. This means:

- You always install the same tested versions
- A compromised newer release cannot silently enter your environment
- The weekly CI audit catches any newly-discovered CVEs in pinned packages

### Run an audit yourself

```
pip install pip-audit
pip-audit -r requirements.txt
```

A clean run looks like:

```
No known vulnerabilities found
```

### How CI works

A GitHub Actions workflow (`.github/workflows/security-audit.yml`) runs automatically:

- On every push to `main`
- On every pull request
- Every Monday at 08:00 UTC (scheduled)

The badge at the top of this file turns **red** if any pinned package has a known CVE.
Dependabot is also configured to open pull requests when upstream versions change.

### Updating dependencies

```
pip install --upgrade textual psutil
pip freeze > requirements.txt
```

Then run `pip-audit -r requirements.txt` to confirm no new issues before committing.

---

## Licence

MIT — see [LICENSE](LICENSE).
