#!/usr/bin/env python3
"""
Battery Extender TUI — Windows 11 power management dashboard.
Run as Administrator for power plan switching, brightness, and CPU throttle.

Install: pip install textual psutil
Run:     python battery_tui.py
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical
    from textual.reactive import reactive
    from textual.widgets import (
        Button, DataTable, Footer, Label,
        RadioButton, RadioSet, Rule, Static,
    )
    from textual import on, work
except ImportError:
    sys.exit("Run: pip install textual psutil")


# Baseline from battery-report.html — update if battery is replaced
_REPORT_DESIGN_MWH = 51_999
_REPORT_FULL_MWH   = 18_992

# WMI GUIDs for CPU throttle setting (processor power management)
_CPU_SUBGROUP = "54533251-82be-4824-96c1-47b60b740d00"
_CPU_MAX_IDX  = "bc5038f7-23e0-4960-96da-33abaf5935ec"


# ── Admin / shell helpers ─────────────────────────────────────────────────────


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps(cmd: str, timeout: int = 8) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NonInteractive", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _run(*args: str) -> bool:
    try:
        return subprocess.run(list(args), capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


# ── Battery ───────────────────────────────────────────────────────────────────


@dataclass
class BatteryData:
    remaining_mwh: int  = 0
    full_mwh: int       = _REPORT_FULL_MWH
    design_mwh: int     = _REPORT_DESIGN_MWH
    discharge_mw: int   = 0
    charge_mw: int      = 0
    charging: bool      = False
    discharging: bool   = False
    plugged: bool       = False
    percent: float      = 0.0
    wmi_ok: bool        = False

    @property
    def health_pct(self) -> float:
        return (self.full_mwh / self.design_mwh * 100) if self.design_mwh else 0.0

    @property
    def watts(self) -> float:
        if self.discharging and self.discharge_mw > 0:
            return self.discharge_mw / 1000.0
        if self.charging and self.charge_mw > 0:
            return -self.charge_mw / 1000.0
        return 0.0

    @property
    def runtime_min(self) -> Optional[float]:
        if not self.discharging or self.discharge_mw <= 0 or self.remaining_mwh <= 0:
            return None
        return (self.remaining_mwh / self.discharge_mw) * 60.0


def _fetch_battery() -> BatteryData:
    """Read battery state via WMI BatteryStatus classes (most accurate on Windows)."""
    out = _ps(r"""
$s  = Get-WmiObject -Ns root\WMI -Class BatteryStatus             -EA SilentlyContinue | Select -First 1
$fc = Get-WmiObject -Ns root\WMI -Class BatteryFullChargedCapacity -EA SilentlyContinue | Select -First 1
$sd = Get-WmiObject -Ns root\WMI -Class BatteryStaticData          -EA SilentlyContinue | Select -First 1
if ($s) {
    $dr = [int]($s.DischargeRate    -as [double])
    $cr = [int]($s.ChargeRate       -as [double])
    $rc = [int]($s.RemainingCapacity -as [double])
    $fv = if ($fc) { [int]$fc.FullChargedCapacity } else { 0 }
    $dv = if ($sd) { [int]$sd.DesignedCapacity }    else { 0 }
    "$rc|$fv|$dv|$dr|$cr|$($s.Charging)|$($s.Discharging)|$($s.PowerOnline)"
}
""")
    d = BatteryData()
    bat = psutil.sensors_battery()
    if bat:
        d.percent  = bat.percent
        d.plugged  = bat.power_plugged

    if out:
        try:
            p = out.split("|")
            rc, fc, dc, dr, cr = int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4])
            d.remaining_mwh = rc
            if fc > 0: d.full_mwh   = fc
            if dc > 0: d.design_mwh = dc
            d.discharge_mw  = dr
            d.charge_mw     = cr
            d.charging      = p[5].strip().lower() == "true"
            d.discharging   = p[6].strip().lower() == "true"
            d.plugged       = p[7].strip().lower() == "true"
            if d.full_mwh > 0 and rc > 0:
                d.percent = rc / d.full_mwh * 100
            d.wmi_ok = True
        except (IndexError, ValueError):
            pass
    return d


# ── Power plans ───────────────────────────────────────────────────────────────


@dataclass
class PowerPlan:
    guid: str
    name: str
    active: bool = False


def _fetch_plans() -> list[PowerPlan]:
    plans_out  = _ps("powercfg /list")
    active_out = _ps("powercfg /getactivescheme")
    m = re.search(r"([0-9a-f-]{36})", active_out, re.I)
    active_guid = m.group(1).lower() if m else ""
    plans: list[PowerPlan] = []
    for line in plans_out.splitlines():
        m = re.search(r"([0-9a-f-]{36})\s+\((.+?)\)", line, re.I)
        if m:
            g = m.group(1).lower()
            plans.append(PowerPlan(guid=g, name=m.group(2).strip(), active=(g == active_guid)))
    return plans


def _activate_plan(guid: str) -> bool:
    return _run("powercfg", "/setactive", guid)


# ── CPU max state (battery) ───────────────────────────────────────────────────


def _get_cpu_max_dc() -> int:
    out = _ps(f"powercfg /query SCHEME_CURRENT {_CPU_SUBGROUP} {_CPU_MAX_IDX}")
    m = re.search(r"Current DC Power Setting Index:\s+0x([0-9a-f]+)", out, re.I)
    return int(m.group(1), 16) if m else 100


def _set_cpu_max_dc(pct: int) -> None:
    pct = max(20, min(100, pct))
    _run("powercfg", "/setdcvalueindex", "SCHEME_CURRENT", _CPU_SUBGROUP, _CPU_MAX_IDX, hex(pct))
    _run("powercfg", "/setactive", "SCHEME_CURRENT")


# ── Brightness ────────────────────────────────────────────────────────────────


def _get_brightness() -> int:
    out = _ps(r"(Get-WmiObject -Ns root\WMI -Class WmiMonitorBrightness -EA SilentlyContinue | Select -First 1).CurrentBrightness")
    try:
        return int(out)
    except ValueError:
        return -1


def _set_brightness(lvl: int) -> None:
    lvl = max(5, min(100, lvl))
    _ps(fr"(Get-WmiObject -Ns root\WMI -Class WmiMonitorBrightnessMethods -EA SilentlyContinue | Select -First 1).WmiSetBrightness(1, {lvl})")


# ── Processes ─────────────────────────────────────────────────────────────────


@dataclass
class ProcInfo:
    pid: int
    name: str
    cpu: float
    mem_mb: float

    @property
    def est_watts(self) -> float:
        return round(self.cpu / 100.0 * 10.0, 1)  # ~10W CPU budget at 100%


def _fetch_procs(n: int = 10) -> list[ProcInfo]:
    procs: list[ProcInfo] = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        try:
            i = p.info
            mem = i["memory_info"].rss / 1_048_576 if i["memory_info"] else 0.0
            procs.append(ProcInfo(
                pid=i["pid"],
                name=(i["name"] or "?")[:28],
                cpu=i["cpu_percent"] or 0.0,
                mem_mb=mem,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x.cpu, reverse=True)
    return procs[:n]


# ── Session energy history ────────────────────────────────────────────────────


@dataclass
class ProcessHistory:
    """Cumulative energy and CPU trend for a process name across the session.

    Multiple instances of the same executable (e.g. all svchost.exe) are
    aggregated here under one entry — that gives the honest total cost.
    """
    name: str
    total_mwh: float = 0.0
    sample_count: int = 0
    # Rolling window of recent CPU% readings (last ~60s at 4s interval = 15 samples)
    recent_cpu: deque = field(default_factory=lambda: deque(maxlen=15))

    @property
    def avg_cpu(self) -> float:
        return sum(self.recent_cpu) / len(self.recent_cpu) if self.recent_cpu else 0.0

    @property
    def trend(self) -> str:
        """↑ rising  → stable  ↓ falling  … too few samples."""
        if len(self.recent_cpu) < 6:
            return "[dim]…[/dim]"
        samples = list(self.recent_cpu)
        mid = len(samples) // 2
        old = sum(samples[:mid]) / mid
        new = sum(samples[mid:]) / (len(samples) - mid)
        if new > old * 1.3:
            return "[red]↑[/red]"
        if new < old * 0.7:
            return "[green]↓[/green]"
        return "[yellow]→[/yellow]"


# ── Formatting ────────────────────────────────────────────────────────────────


def _bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _rag_bar(value: float, width: int = 22) -> str:
    """Green-Amber-Red bar (green=low, red=high). ▼ marks current value.

    Lower values sit in the green zone (better for battery life for both
    brightness and CPU limit). The ▼ marker floats on top of the zone color.
    """
    value = max(0.0, min(100.0, value))
    pos = min(int(round(value / 100 * width)), width - 1)
    parts: list[str] = []
    for i in range(width):
        frac = (i + 0.5) / width
        color = "green" if frac < 0.34 else "yellow" if frac < 0.67 else "red"
        if i == pos:
            # White marker sits visibly over any zone color
            parts.append("[bold white]▼[/bold white]")
        elif i < pos:
            parts.append(f"[{color}]█[/{color}]")
        else:
            parts.append(f"[{color}]░[/{color}]")
    return "".join(parts) + f"  [bold]{value:.0f}%[/bold]"


def _fmt_mwh(v: int) -> str:
    return f"{v:,}" if v else "—"


def _fmt_runtime(mins: Optional[float]) -> str:
    if mins is None:
        return "—"
    h, m = int(mins // 60), int(mins % 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _health_markup(h: float) -> str:
    if h >= 80:
        return f"[green]{h:.0f}%[/green]"
    if h >= 50:
        return f"[yellow]{h:.0f}%[/yellow]"
    return f"[red]{h:.0f}%[/red]"


def _draw_markup(watts: float, discharging: bool) -> str:
    if not discharging:
        arrow = "[green]↑ charging[/green]" if watts < 0 else "[green]AC[/green]"
        return f"[green]{abs(watts):.1f} W[/green]  {arrow}"
    color = "red" if watts > 18 else "yellow" if watts > 12 else "green"
    warn  = "  [red]⚠ HIGH[/red]" if watts > 18 else ""
    return f"[{color}]{watts:.1f} W[/{color}]{warn}"


def _fmt_session(secs: float) -> str:
    h, m, s = int(secs // 3600), int((secs % 3600) // 60), int(secs % 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


# ── CSS ───────────────────────────────────────────────────────────────────────


_CSS = """
Screen { layout: vertical; background: $background; }

#top-bar {
    height: 4;
    layout: horizontal;
    background: $panel;
    border-bottom: solid $accent;
    padding: 0 2;
}
.top-cell        { width: 1fr; content-align: center middle; }
.top-cell.right  { content-align: right middle; }
.top-cell.left   { content-align: left middle; }

#main-row { layout: horizontal; height: 1fr; }

#left-panel {
    width: 44;
    border: solid $accent;
    margin: 1 0 0 1;
    padding: 1 2;
    overflow-y: auto;
}

#right-panel {
    border: solid $accent;
    margin: 1 1 0 1;
    padding: 1 2;
    width: 1fr;
}

/* Bottom row: current processes | session energy */
#bottom-row {
    layout: horizontal;
    height: 17;
    margin: 1 1 0 1;
}

.bottom-panel {
    width: 1fr;
    border: solid $accent;
    padding: 1 1 0 1;
    margin: 0 0 0 0;
}

#proc-panel  { margin-right: 1; }
#energy-panel { }

.sec { color: $accent; text-style: bold; margin-bottom: 1; }

.stat         { height: 1; layout: horizontal; }
.slbl         { width: 24; color: $text-muted; }
.sval         { width: 1fr; text-style: bold; }

.btn-row        { layout: horizontal; margin: 0 0 0 0; height: 3; }
.btn-row Button { margin: 0 1 0 0; min-width: 7; }

.rag-bar { margin: 0 0 1 0; height: 1; }

#plan-radio { margin: 1 0; }

DataTable { height: 1fr; }

.admin-warn {
    background: $warning 20%;
    color: $warning;
    text-style: bold;
    padding: 0 2;
    height: 1;
    content-align: left middle;
}

#status-line {
    height: 1;
    background: $panel;
    border-top: solid $primary-background-darken-1;
    padding: 0 2;
    color: $text-muted;
    content-align: left middle;
}
"""


# ── App ───────────────────────────────────────────────────────────────────────


class BatteryExtenderApp(App):
    """Windows 11 battery management TUI."""

    CSS = _CSS
    TITLE = "Battery Extender"
    BINDINGS = [
        Binding("q",        "quit",           "Quit"),
        Binding("r",        "refresh",        "Refresh"),
        Binding("s",        "do_sleep",       "Sleep"),
        Binding("h",        "do_hibernate",   "Hibernate"),
        Binding("b",        "bright_up",      "Bright +10"),
        Binding("shift+b",  "bright_down",    "Bright −10"),
    ]

    _battery:        BatteryData     = BatteryData()
    _plans:          list[PowerPlan] = []
    _procs:          list[ProcInfo]  = []
    _brightness:     int             = -1
    _cpu_max_dc:     int             = 100
    _admin:          bool            = False
    _initialized:    bool            = False

    # Session energy tracking
    _energy_history: dict            = {}   # name.lower() -> ProcessHistory
    _session_start:  float           = 0.0
    _last_sample_t:  float           = 0.0

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        self._admin = is_admin()  # must be set before the conditional below

        # Top bar
        with Container(id="top-bar"):
            yield Static("", id="top-bat",    classes="top-cell left")
            yield Static("", id="top-health", classes="top-cell")
            yield Static("", id="top-draw",   classes="top-cell right")

        if not self._admin:
            yield Static(
                " ⚠  Not running as Administrator — power plan, brightness, and CPU throttle controls are read-only.",
                classes="admin-warn",
            )

        # Main row: left controls | right stats
        with Horizontal(id="main-row"):
            with Container(id="left-panel"):
                yield Static("POWER PROFILE", classes="sec")
                yield RadioSet(id="plan-radio")

                yield Rule()
                yield Static("BATTERY CPU LIMIT (on battery)", classes="sec")
                yield Static("", id="cpu-rag", classes="rag-bar")
                with Horizontal(classes="btn-row"):
                    for pct in (100, 80, 60, 40):
                        yield Button(f"{pct}%", id=f"cpu-{pct}", variant="default")

                yield Rule()
                yield Static("SCREEN BRIGHTNESS", classes="sec")
                yield Static("", id="bright-rag", classes="rag-bar")
                with Horizontal(classes="btn-row"):
                    for btn_id, label in (("bright-n10", "−10"), ("bright-n5", "−5"),
                                          ("bright-p5", "+5"), ("bright-p10", "+10")):
                        yield Button(label, id=btn_id)

                yield Rule()
                yield Static("QUICK ACTIONS", classes="sec")
                with Horizontal(classes="btn-row"):
                    yield Button("Sleep",     id="btn-sleep",     variant="warning")
                    yield Button("Hibernate", id="btn-hibernate", variant="warning")

            with Container(id="right-panel"):
                yield Static("SYSTEM STATUS", classes="sec")
                for row_id in ("row-bat", "row-full", "row-design", "row-health",
                               "row-draw", "row-eta", "row-cpu", "row-ram",
                               "row-warn"):
                    with Horizontal(classes="stat"):
                        yield Static("", id=f"lbl-{row_id}", classes="slbl")
                        yield Static("", id=f"val-{row_id}", classes="sval")

        # Bottom row: snapshot table | cumulative session energy table
        with Horizontal(id="bottom-row"):
            with Container(id="proc-panel", classes="bottom-panel"):
                yield Static("CURRENT SNAPSHOT  (CPU-weighted, ~10W per 100%)", classes="sec")
                tbl = DataTable(id="proc-table", zebra_stripes=True)
                tbl.add_columns("Process", "PID", "CPU%", "RAM MB", "Est. W")
                yield tbl

            with Container(id="energy-panel", classes="bottom-panel"):
                yield Static("", id="energy-title", classes="sec")
                etbl = DataTable(id="energy-table", zebra_stripes=True)
                etbl.add_columns("Process", "Total mWh", "Avg CPU%", "Trend")
                yield etbl

        yield Static("", id="status-line")
        yield Footer()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._session_start = time.time()
        # Prime psutil CPU measurement (first call always returns 0)
        psutil.cpu_percent(interval=None)
        for p in psutil.process_iter(["cpu_percent"]):
            try:
                p.cpu_percent(interval=None)
            except Exception:
                pass
        self._fetch_all()
        self.set_interval(4, self._fetch_all)

    # ── Data fetch (background thread) ───────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _fetch_all(self) -> None:
        bat     = _fetch_battery()
        plans   = _fetch_plans()
        procs   = _fetch_procs()
        bright  = _get_brightness()
        cpu_max = _get_cpu_max_dc() if self._admin else self._cpu_max_dc
        self.call_from_thread(self._apply, bat, plans, procs, bright, cpu_max)

    def _apply(
        self,
        bat: BatteryData,
        plans: list[PowerPlan],
        procs: list[ProcInfo],
        bright: int,
        cpu_max: int,
    ) -> None:
        self._battery    = bat
        self._plans      = plans
        self._procs      = procs
        self._brightness = bright
        self._cpu_max_dc = cpu_max

        self._accumulate_energy(procs)
        self._refresh_ui()

        if not self._initialized:
            self._build_plan_radio()
            self._initialized = True

    # ── Energy accumulation ───────────────────────────────────────────────────

    def _accumulate_energy(self, procs: list[ProcInfo]) -> None:
        """Add this sample's energy contribution to each process's running total.

        Processes are aggregated by name so all svchost.exe instances (which
        Windows splits across many PIDs) are summed into a single honest total.
        """
        now = time.time()
        if self._last_sample_t > 0:
            elapsed_h = (now - self._last_sample_t) / 3600.0

            # Aggregate CPU% by name first (handles multi-instance processes)
            by_name: dict[str, float] = {}
            for p in procs:
                key = p.name.lower()
                by_name[key] = by_name.get(key, 0.0) + p.cpu

            for key, total_cpu in by_name.items():
                if key not in self._energy_history:
                    # Use the display name from any matching proc
                    display = next((p.name for p in procs if p.name.lower() == key), key)
                    self._energy_history[key] = ProcessHistory(name=display)
                hist = self._energy_history[key]
                hist.total_mwh    += (total_cpu / 100.0) * 10.0 * elapsed_h * 1000  # mWh
                hist.sample_count += 1
                hist.recent_cpu.append(total_cpu)

        self._last_sample_t = now

    # ── UI update ─────────────────────────────────────────────────────────────

    def _refresh_ui(self) -> None:
        bat = self._battery

        # ── Top bar
        charge_bar = _bar(bat.percent, 22)
        self.query_one("#top-bat", Static).update(
            f"[bold]{charge_bar}[/bold]  [bold]{bat.percent:.0f}%[/bold]"
        )
        self.query_one("#top-health", Static).update(
            f"Health: {_health_markup(bat.health_pct)}"
        )
        draw_str = _draw_markup(bat.watts, bat.discharging)
        eta_str  = _fmt_runtime(bat.runtime_min)
        self.query_one("#top-draw", Static).update(
            f"{draw_str}   Est: [bold]{eta_str}[/bold]"
        )

        # ── Right panel stats
        self._stat("row-bat",    "Battery remaining",
                   f"{_fmt_mwh(bat.remaining_mwh)} / {_fmt_mwh(bat.full_mwh)} mWh  ({bat.percent:.0f}%)")
        self._stat("row-full",   "Full charge cap",    f"{_fmt_mwh(bat.full_mwh)} mWh")
        self._stat("row-design", "Design capacity",    f"{_fmt_mwh(bat.design_mwh)} mWh")
        self._stat("row-health", "Battery health",
                   _health_markup(bat.health_pct)
                   + ("  [red]⚠ critical — consider replacement[/red]" if bat.health_pct < 50 else ""))
        self._stat("row-draw",   "Power draw",
                   _draw_markup(bat.watts, bat.discharging) if bat.wmi_ok else "[dim]WMI unavailable[/dim]")
        self._stat("row-eta",    "Est. runtime",       f"[bold]{_fmt_runtime(bat.runtime_min)}[/bold]")
        self._stat("row-cpu",    "CPU load",           f"{psutil.cpu_percent(interval=None):.0f}%")
        vm = psutil.virtual_memory()
        self._stat("row-ram",    "RAM",
                   f"{vm.used/1073741824:.1f} / {vm.total/1073741824:.1f} GB")

        warn = ""
        if bat.discharging and bat.watts > 18:
            warn = f"[red]⚠ High draw ({bat.watts:.0f}W) — close heavy apps or lower CPU limit[/red]"
        elif bat.discharging and bat.watts > 12:
            warn = f"[yellow]Moderate draw ({bat.watts:.0f}W) — brightness -10% saves ~1–2W[/yellow]"
        elif not bat.discharging:
            gain = round((bat.design_mwh - bat.full_mwh) / 1000, 1)
            warn = f"[dim]On AC · Battery has lost {gain:.0f} Wh ({bat.health_pct:.0f}% health)[/dim]"
        self._stat("row-warn", "", warn)

        # ── RAG bars
        bright_val = self._brightness if self._brightness >= 0 else 0
        bright_rag = _rag_bar(bright_val) if self._brightness >= 0 else "[dim]unavailable[/dim]"
        self.query_one("#bright-rag", Static).update(bright_rag)
        self.query_one("#cpu-rag",    Static).update(_rag_bar(self._cpu_max_dc))

        # ── Sync active plan radio
        self._sync_active_plan()

        # ── Current snapshot process table
        tbl = self.query_one("#proc-table", DataTable)
        tbl.clear()
        for p in self._procs:
            color = "red" if p.cpu > 30 else "yellow" if p.cpu > 10 else "white"
            tbl.add_row(
                p.name,
                str(p.pid),
                f"[{color}]{p.cpu:.1f}%[/{color}]",
                f"{p.mem_mb:,.0f}",
                f"~{p.est_watts:.1f}" if p.est_watts >= 0.1 else "<0.1",
            )

        # ── Session energy table
        session_secs = time.time() - self._session_start
        self.query_one("#energy-title", Static).update(
            f"SESSION ENERGY  ({_fmt_session(session_secs)}  ·  {len(self._energy_history)} processes seen)"
        )
        etbl = self.query_one("#energy-table", DataTable)
        etbl.clear()
        ranked = sorted(self._energy_history.values(), key=lambda h: h.total_mwh, reverse=True)
        for hist in ranked[:12]:
            mwh_str  = f"{hist.total_mwh:.2f}" if hist.total_mwh >= 0.01 else "<0.01"
            avg_str  = f"{hist.avg_cpu:.1f}%"
            color    = "red" if hist.avg_cpu > 30 else "yellow" if hist.avg_cpu > 10 else "white"
            etbl.add_row(
                hist.name,
                f"[{color}]{mwh_str}[/{color}]",
                f"[{color}]{avg_str}[/{color}]",
                hist.trend,
            )

        # ── Status line
        ts  = time.strftime("%H:%M:%S")
        src = "WMI" if self._battery.wmi_ok else "psutil"
        self.query_one("#status-line", Static).update(
            f"Updated {ts} via {src}  ·  r refresh  ·  b/B brightness  ·  Admin: {'yes' if self._admin else 'NO'}"
        )

    def _stat(self, row_id: str, label: str, value: str) -> None:
        self.query_one(f"#lbl-{row_id}", Static).update(label)
        self.query_one(f"#val-{row_id}", Static).update(value)

    def _build_plan_radio(self) -> None:
        radio = self.query_one("#plan-radio", RadioSet)
        radio.remove_children()
        for plan in self._plans:
            radio.mount(RadioButton(plan.name, value=plan.active, id=f"plan-{plan.guid}"))

    def _sync_active_plan(self) -> None:
        if not self._initialized:
            return
        for plan in self._plans:
            try:
                self.query_one(f"#plan-{plan.guid}", RadioButton).value = plan.active
            except Exception:
                pass

    # ── Event handlers ────────────────────────────────────────────────────────

    @on(RadioSet.Changed, "#plan-radio")
    def _on_plan_changed(self, event: RadioSet.Changed) -> None:
        if not self._admin:
            self._set_status("⚠ Administrator required to switch power plans.")
            return
        label = str(event.pressed.label)
        guid  = next((p.guid for p in self._plans if p.name == label), None)
        if guid:
            self._set_status(f"Switching to '{label}'…")
            self._apply_plan(guid, label)

    @work(thread=True)
    def _apply_plan(self, guid: str, name: str) -> None:
        ok = _activate_plan(guid)
        msg = f"Power plan set to '{name}'." if ok else f"Failed to set plan '{name}'."
        self.call_from_thread(self._set_status, msg)
        self.call_from_thread(self._fetch_all)

    @on(Button.Pressed, "#cpu-100")
    def _cpu100(self) -> None: self._do_cpu_max(100)

    @on(Button.Pressed, "#cpu-80")
    def _cpu80(self) -> None: self._do_cpu_max(80)

    @on(Button.Pressed, "#cpu-60")
    def _cpu60(self) -> None: self._do_cpu_max(60)

    @on(Button.Pressed, "#cpu-40")
    def _cpu40(self) -> None: self._do_cpu_max(40)

    def _do_cpu_max(self, pct: int) -> None:
        if not self._admin:
            self._set_status("⚠ Administrator required to change CPU limits.")
            return
        self._set_status(f"Setting CPU battery limit to {pct}%…")
        self._apply_cpu_max(pct)

    @work(thread=True)
    def _apply_cpu_max(self, pct: int) -> None:
        _set_cpu_max_dc(pct)
        self.call_from_thread(self._set_status, f"CPU battery limit set to {pct}%.")
        self.call_from_thread(self._fetch_all)

    @on(Button.Pressed, "#bright-n10")
    def _b_m10(self) -> None: self._do_bright(-10)

    @on(Button.Pressed, "#bright-n5")
    def _b_m5(self) -> None: self._do_bright(-5)

    @on(Button.Pressed, "#bright-p5")
    def _b_p5(self) -> None: self._do_bright(5)

    @on(Button.Pressed, "#bright-p10")
    def _b_p10(self) -> None: self._do_bright(10)

    def _do_bright(self, delta: int) -> None:
        if not self._admin:
            self._set_status("⚠ Administrator required to set brightness.")
            return
        new_lvl = max(5, min(100, (self._brightness if self._brightness >= 0 else 75) + delta))
        self._set_status(f"Setting brightness to {new_lvl}%…")
        self._apply_bright(new_lvl)

    @work(thread=True)
    def _apply_bright(self, lvl: int) -> None:
        _set_brightness(lvl)
        new = _get_brightness()
        self.call_from_thread(self._set_status, f"Brightness set to {new}%.")
        self._brightness = new
        self.call_from_thread(self._refresh_ui)

    @on(Button.Pressed, "#btn-sleep")
    def _on_sleep(self) -> None: self.action_do_sleep()

    @on(Button.Pressed, "#btn-hibernate")
    def _on_hibernate(self) -> None: self.action_do_hibernate()

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._set_status("Refreshing…")
        self._fetch_all()

    def action_do_sleep(self) -> None:
        self._set_status("Sleeping in 2 seconds…")
        self._sleep_after(2)

    @work(thread=True)
    def _sleep_after(self, secs: int) -> None:
        time.sleep(secs)
        _run("rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0")

    def action_do_hibernate(self) -> None:
        self._set_status("Hibernating in 2 seconds…")
        self._hibernate_after(2)

    @work(thread=True)
    def _hibernate_after(self, secs: int) -> None:
        time.sleep(secs)
        _run("shutdown", "/h")

    def action_bright_up(self) -> None:   self._do_bright(10)
    def action_bright_down(self) -> None: self._do_bright(-10)

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#status-line", Static).update(msg)
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not is_admin():
        print(
            "\n  ⚠  Not running as Administrator.\n"
            "     Read-only mode: stats and process view work, but you cannot\n"
            "     switch power plans, adjust brightness, or throttle CPU.\n"
            "\n  To enable all features, run from an elevated prompt:\n"
            "     python battery_tui.py\n"
        )

    BatteryExtenderApp().run()
