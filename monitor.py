#!/usr/bin/env python3
"""Linux System Monitor with Telegram Bot and Rich TUI."""

import asyncio
import csv
import io
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ─── Config ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
COLLECT_INTERVAL = 30       # seconds between metric snapshots
DISPLAY_INTERVAL = 5        # seconds between TUI refreshes
HISTORY_MINUTES = 60        # minutes of history to keep
HISTORY_SIZE = (HISTORY_MINUTES * 60) // COLLECT_INTERVAL
CSV_PATH = Path("metrics_history.csv")

DEFAULT_ALERTS: dict[str, float] = {
    "cpu": 90.0,
    "ram": 90.0,
    "disk": 95.0,
    "temp_cpu": 85.0,
}

ASCII_CHART_WIDTH = 60
ASCII_CHART_HEIGHT = 10

# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class Metrics:
    timestamp: datetime = field(default_factory=datetime.now)
    cpu_percent: float = 0.0
    cpu_freq_mhz: float = 0.0
    cpu_count: int = 0
    ram_total_gb: float = 0.0
    ram_used_gb: float = 0.0
    ram_percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_gb: float = 0.0
    swap_percent: float = 0.0
    disks: list[dict] = field(default_factory=list)
    temps: dict[str, list[dict]] = field(default_factory=dict)
    net_sent_mb: float = 0.0
    net_recv_mb: float = 0.0
    net_sent_rate_kbps: float = 0.0
    net_recv_rate_kbps: float = 0.0
    net_interfaces: dict[str, dict] = field(default_factory=dict)
    per_cpu: list[float] = field(default_factory=list)


class MetricsCollector:
    def __init__(self):
        self.history: deque[Metrics] = deque(maxlen=HISTORY_SIZE)
        self.alert_thresholds = dict(DEFAULT_ALERTS)
        self._prev_net = psutil.net_io_counters()
        self._prev_net_time = time.monotonic()
        self._last_alert_times: dict[str, float] = {}
        self.alert_cooldown = 300  # 5 minutes between same alerts
        self.bot_chat_ids: set[int] = set()
        self.bot: Optional[Bot] = None

    def collect(self) -> Metrics:
        m = Metrics()
        m.timestamp = datetime.now()

        # CPU
        m.cpu_percent = psutil.cpu_percent(interval=1)
        m.per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        freq = psutil.cpu_freq()
        m.cpu_freq_mhz = freq.current if freq else 0.0
        m.cpu_count = psutil.cpu_count()

        # RAM
        ram = psutil.virtual_memory()
        m.ram_total_gb = ram.total / 1e9
        m.ram_used_gb = ram.used / 1e9
        m.ram_percent = ram.percent
        swap = psutil.swap_memory()
        m.swap_total_gb = swap.total / 1e9
        m.swap_used_gb = swap.used / 1e9
        m.swap_percent = swap.percent

        # Disks
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                m.disks.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "total_gb": usage.total / 1e9,
                    "used_gb": usage.used / 1e9,
                    "free_gb": usage.free / 1e9,
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                pass

        # Temperatures
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for sensor_name, entries in temps.items():
                    m.temps[sensor_name] = [
                        {"label": e.label or sensor_name, "current": e.current,
                         "high": e.high, "critical": e.critical}
                        for e in entries
                    ]
        except (AttributeError, OSError):
            pass

        # Network
        net = psutil.net_io_counters()
        now = time.monotonic()
        dt = now - self._prev_net_time
        if dt > 0:
            m.net_sent_rate_kbps = (net.bytes_sent - self._prev_net.bytes_sent) / dt / 1024
            m.net_recv_rate_kbps = (net.bytes_recv - self._prev_net.bytes_recv) / dt / 1024
        self._prev_net = net
        self._prev_net_time = now
        m.net_sent_mb = net.bytes_sent / 1e6
        m.net_recv_mb = net.bytes_recv / 1e6

        iface_stats = psutil.net_io_counters(pernic=True)
        iface_addrs = psutil.net_if_addrs()
        for iface, stats in iface_stats.items():
            addrs = [str(a.address) for a in iface_addrs.get(iface, [])
                     if ":" not in str(a.address)]
            m.net_interfaces[iface] = {
                "sent_mb": stats.bytes_sent / 1e6,
                "recv_mb": stats.bytes_recv / 1e6,
                "addrs": addrs,
            }

        self.history.append(m)
        self._append_csv(m)
        return m

    def _append_csv(self, m: Metrics):
        write_header = not CSV_PATH.exists()
        with open(CSV_PATH, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow([
                    "timestamp", "cpu_percent", "cpu_freq_mhz",
                    "ram_percent", "swap_percent", "net_sent_kbps", "net_recv_kbps"
                ])
            writer.writerow([
                m.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                round(m.cpu_percent, 1),
                round(m.cpu_freq_mhz, 0),
                round(m.ram_percent, 1),
                round(m.swap_percent, 1),
                round(m.net_sent_rate_kbps, 1),
                round(m.net_recv_rate_kbps, 1),
            ])

    def check_alerts(self, m: Metrics) -> list[str]:
        alerts = []
        now = time.monotonic()

        def _cooldown_ok(key: str) -> bool:
            last = self._last_alert_times.get(key, 0)
            if now - last >= self.alert_cooldown:
                self._last_alert_times[key] = now
                return True
            return False

        if m.cpu_percent > self.alert_thresholds["cpu"] and _cooldown_ok("cpu"):
            alerts.append(f"🔥 CPU перегрузка: {m.cpu_percent:.1f}% > {self.alert_thresholds['cpu']}%")

        if m.ram_percent > self.alert_thresholds["ram"] and _cooldown_ok("ram"):
            alerts.append(f"🧠 RAM перегрузка: {m.ram_percent:.1f}% > {self.alert_thresholds['ram']}%")

        for disk in m.disks:
            key = f"disk_{disk['mountpoint']}"
            if disk["percent"] > self.alert_thresholds["disk"] and _cooldown_ok(key):
                alerts.append(
                    f"💾 Диск {disk['mountpoint']} заполнен: {disk['percent']}% > {self.alert_thresholds['disk']}%"
                )

        cpu_temp = _get_cpu_temp(m)
        if cpu_temp and cpu_temp > self.alert_thresholds["temp_cpu"] and _cooldown_ok("temp_cpu"):
            alerts.append(f"🌡️ Температура CPU: {cpu_temp:.1f}°C > {self.alert_thresholds['temp_cpu']}°C")

        return alerts

    def latest(self) -> Optional[Metrics]:
        return self.history[-1] if self.history else None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_cpu_temp(m: Metrics) -> Optional[float]:
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
        if key in m.temps:
            vals = [e["current"] for e in m.temps[key]]
            if vals:
                return max(vals)
    for entries in m.temps.values():
        for e in entries:
            if "cpu" in e["label"].lower() or "package" in e["label"].lower():
                return e["current"]
    return None


def _bar(percent: float, width: int = 20) -> str:
    filled = int(percent / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    if percent >= 90:
        color = "red"
    elif percent >= 70:
        color = "yellow"
    else:
        color = "green"
    return f"[{color}]{bar}[/{color}] {percent:5.1f}%"


def _ascii_chart(values: list[float], title: str, unit: str = "%") -> str:
    if not values:
        return f"{title}: нет данных"
    w = min(ASCII_CHART_WIDTH, len(values))
    data = list(values)[-w:]
    max_val = max(data) if data else 100
    scale = max_val if max_val > 0 else 100
    rows = []
    rows.append(f"┌─ {title} (макс: {max_val:.1f}{unit}) " + "─" * (w - len(title) - 14) + "┐")
    for row in range(ASCII_CHART_HEIGHT, 0, -1):
        threshold = (row / ASCII_CHART_HEIGHT) * scale
        line = "│"
        for v in data:
            line += "█" if v >= threshold else " "
        line += "│"
        rows.append(line)
    rows.append("└" + "─" * w + "┘")
    rows.append(f"  {'<─── последний час ───>'[:w]}")
    return "\n".join(rows)


def _fmt_bytes(mb: float) -> str:
    if mb >= 1024:
        return f"{mb/1024:.2f} ГБ"
    return f"{mb:.1f} МБ"


# ─── Rich TUI ─────────────────────────────────────────────────────────────────

class Dashboard:
    def __init__(self, collector: MetricsCollector):
        self.collector = collector
        self.console = Console()

    def _make_cpu_panel(self, m: Metrics) -> Panel:
        t = Table.grid(padding=(0, 1))
        t.add_column(style="bold cyan", min_width=20)
        t.add_column()
        t.add_row("Загрузка:", _bar(m.cpu_percent))
        t.add_row("Частота:", f"{m.cpu_freq_mhz:.0f} МГц")
        t.add_row("Ядра:", str(m.cpu_count))
        if m.per_cpu:
            chunks = [m.per_cpu[i:i+4] for i in range(0, len(m.per_cpu), 4)]
            for chunk in chunks:
                t.add_row("", "  ".join(f"CPU{i}: {v:5.1f}%" for i, v in enumerate(chunk)))
        return Panel(t, title="[bold]CPU", border_style="cyan")

    def _make_ram_panel(self, m: Metrics) -> Panel:
        t = Table.grid(padding=(0, 1))
        t.add_column(style="bold magenta", min_width=20)
        t.add_column()
        t.add_row("ОЗУ:", _bar(m.ram_percent))
        t.add_row("Использовано:", f"{m.ram_used_gb:.2f} / {m.ram_total_gb:.2f} ГБ")
        t.add_row("Своп:", _bar(m.swap_percent))
        t.add_row("Своп исп.:", f"{m.swap_used_gb:.2f} / {m.swap_total_gb:.2f} ГБ")
        return Panel(t, title="[bold]Память", border_style="magenta")

    def _make_disk_panel(self, m: Metrics) -> Panel:
        t = Table(show_header=True, header_style="bold yellow")
        t.add_column("Устройство")
        t.add_column("Точка монтирования")
        t.add_column("ФС")
        t.add_column("Всего", justify="right")
        t.add_column("Исп.", justify="right")
        t.add_column("Загрузка", min_width=28)
        for d in m.disks:
            color = "red" if d["percent"] > 95 else "yellow" if d["percent"] > 80 else "green"
            t.add_row(
                d["device"], d["mountpoint"], d["fstype"],
                f"{d['total_gb']:.1f} ГБ",
                f"{d['used_gb']:.1f} ГБ",
                Text(_bar(d["percent"]).replace("[", "").replace("]", ""), style=color),
            )
        return Panel(t, title="[bold]Диски", border_style="yellow")

    def _make_temp_panel(self, m: Metrics) -> Panel:
        t = Table.grid(padding=(0, 1))
        t.add_column(style="bold red", min_width=20)
        t.add_column()
        if not m.temps:
            t.add_row("Сенсоры:", "не обнаружены")
        else:
            for sensor, entries in m.temps.items():
                for e in entries:
                    label = e["label"] or sensor
                    color = "red" if e["current"] > 85 else "yellow" if e["current"] > 70 else "green"
                    t.add_row(f"{label}:", f"[{color}]{e['current']:.1f}°C[/{color}]")
        return Panel(t, title="[bold]Температура", border_style="red")

    def _make_net_panel(self, m: Metrics) -> Panel:
        t = Table.grid(padding=(0, 1))
        t.add_column(style="bold blue", min_width=20)
        t.add_column()
        t.add_row("↑ Отправлено (всего):", _fmt_bytes(m.net_sent_mb))
        t.add_row("↓ Получено (всего):", _fmt_bytes(m.net_recv_mb))
        t.add_row("↑ Скорость:", f"{m.net_sent_rate_kbps:.1f} КБ/с")
        t.add_row("↓ Скорость:", f"{m.net_recv_rate_kbps:.1f} КБ/с")
        for iface, info in list(m.net_interfaces.items())[:6]:
            t.add_row(f"  {iface}:", ", ".join(info["addrs"]) or "нет адреса")
        return Panel(t, title="[bold]Сеть", border_style="blue")

    def _make_header(self, m: Metrics) -> Panel:
        ts = m.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        host = os.uname().nodename
        content = Text.from_markup(
            f"[bold white]🖥  Мониторинг системы[/]  [dim]│[/]  "
            f"[cyan]{host}[/]  [dim]│[/]  [green]{ts}[/]  [dim]│[/]  "
            f"[yellow]Обновление каждые {DISPLAY_INTERVAL}с[/]"
        )
        return Panel(content, border_style="white")

    def make_layout(self) -> Layout:
        m = self.collector.latest()
        if m is None:
            return Layout(Panel("Сбор данных...", border_style="white"))

        layout = Layout()
        layout.split_column(
            Layout(self._make_header(m), size=3),
            Layout(name="top", ratio=1),
            Layout(name="mid", ratio=1),
            Layout(self._make_disk_panel(m), ratio=1),
        )
        layout["top"].split_row(
            Layout(self._make_cpu_panel(m)),
            Layout(self._make_ram_panel(m)),
        )
        layout["mid"].split_row(
            Layout(self._make_temp_panel(m)),
            Layout(self._make_net_panel(m)),
        )
        return layout


# ─── Telegram formatting ──────────────────────────────────────────────────────

def _tg_status(m: Metrics) -> str:
    cpu_temp = _get_cpu_temp(m)
    temp_str = f"{cpu_temp:.1f}°C" if cpu_temp else "н/д"
    lines = [
        f"🖥 *Дашборд системы*",
        f"🕐 `{m.timestamp.strftime('%H:%M:%S')}`",
        "",
        f"⚡ *CPU:* `{m.cpu_percent:.1f}%`  |  `{m.cpu_freq_mhz:.0f} МГц`  |  🌡 `{temp_str}`",
        f"🧠 *RAM:* `{m.ram_percent:.1f}%`  (`{m.ram_used_gb:.2f}` / `{m.ram_total_gb:.2f}` ГБ)",
        f"💤 *Своп:* `{m.swap_percent:.1f}%`  (`{m.swap_used_gb:.2f}` / `{m.swap_total_gb:.2f}` ГБ)",
        "",
        "💾 *Диски:*",
    ]
    for d in m.disks:
        icon = "🔴" if d["percent"] > 95 else "🟡" if d["percent"] > 80 else "🟢"
        lines.append(f"  {icon} `{d['mountpoint']}` — `{d['percent']}%` (`{d['used_gb']:.1f}`/`{d['total_gb']:.1f}` ГБ)")
    lines += [
        "",
        "🌐 *Сеть:*",
        f"  ↑ `{m.net_sent_rate_kbps:.1f}` КБ/с  |  ↓ `{m.net_recv_rate_kbps:.1f}` КБ/с",
    ]
    return "\n".join(lines)


def _tg_cpu(m: Metrics) -> str:
    lines = [
        "⚡ *CPU — Подробно*",
        f"Загрузка: `{m.cpu_percent:.1f}%`",
        f"Частота: `{m.cpu_freq_mhz:.0f} МГц`",
        f"Ядра: `{m.cpu_count}`",
        "",
        "По ядрам:",
    ]
    for i, v in enumerate(m.per_cpu):
        bar = "█" * int(v / 10) + "░" * (10 - int(v / 10))
        lines.append(f"  CPU{i}: `{bar}` `{v:5.1f}%`")
    return "\n".join(lines)


def _tg_ram(m: Metrics) -> str:
    return "\n".join([
        "🧠 *Память — Подробно*",
        f"ОЗУ:  `{m.ram_percent:.1f}%`  (`{m.ram_used_gb:.2f}` / `{m.ram_total_gb:.2f}` ГБ)",
        f"Своп: `{m.swap_percent:.1f}%`  (`{m.swap_used_gb:.2f}` / `{m.swap_total_gb:.2f}` ГБ)",
    ])


def _tg_disk(m: Metrics) -> str:
    lines = ["💾 *Диски — Все разделы*", ""]
    for d in m.disks:
        icon = "🔴" if d["percent"] > 95 else "🟡" if d["percent"] > 80 else "🟢"
        lines += [
            f"{icon} *{d['mountpoint']}*",
            f"  Устройство: `{d['device']}`  ФС: `{d['fstype']}`",
            f"  Занято: `{d['used_gb']:.2f}` ГБ из `{d['total_gb']:.2f}` ГБ (`{d['percent']}%`)",
            f"  Свободно: `{d['free_gb']:.2f}` ГБ",
            "",
        ]
    return "\n".join(lines)


def _tg_temp(m: Metrics) -> str:
    lines = ["🌡️ *Температуры*", ""]
    if not m.temps:
        lines.append("Сенсоры не обнаружены.")
        return "\n".join(lines)
    for sensor, entries in m.temps.items():
        lines.append(f"*{sensor}:*")
        for e in entries:
            icon = "🔴" if e["current"] > 85 else "🟡" if e["current"] > 70 else "🟢"
            label = e["label"] or sensor
            high_str = f"  макс `{e['high']}°C`" if e["high"] else ""
            lines.append(f"  {icon} `{label}`: `{e['current']:.1f}°C`{high_str}")
        lines.append("")
    return "\n".join(lines)


def _tg_net(m: Metrics) -> str:
    lines = [
        "🌐 *Сеть*",
        "",
        f"↑ Отправлено (всего): `{_fmt_bytes(m.net_sent_mb)}`",
        f"↓ Получено (всего): `{_fmt_bytes(m.net_recv_mb)}`",
        f"↑ Скорость: `{m.net_sent_rate_kbps:.1f}` КБ/с",
        f"↓ Скорость: `{m.net_recv_rate_kbps:.1f}` КБ/с",
        "",
        "*Интерфейсы:*",
    ]
    for iface, info in m.net_interfaces.items():
        addrs = ", ".join(info["addrs"]) or "нет адреса"
        lines.append(
            f"  `{iface}`: ↑`{_fmt_bytes(info['sent_mb'])}` ↓`{_fmt_bytes(info['recv_mb'])}`  {addrs}"
        )
    return "\n".join(lines)


def _tg_history(collector: MetricsCollector) -> str:
    history = list(collector.history)
    if len(history) < 2:
        return "История ещё накапливается..."
    cpu_vals = [h.cpu_percent for h in history]
    ram_vals = [h.ram_percent for h in history]
    net_up = [h.net_sent_rate_kbps for h in history]

    buf = io.StringIO()
    buf.write("📊 *История метрик (последний час)*\n\n")
    buf.write("```\n")
    buf.write(_ascii_chart(cpu_vals, "CPU %", "%") + "\n\n")
    buf.write(_ascii_chart(ram_vals, "RAM %", "%") + "\n\n")
    buf.write(_ascii_chart(net_up, "Сеть ↑ КБ/с", " КБ/с"))
    buf.write("\n```")
    return buf.getvalue()


def _tg_alerts(collector: MetricsCollector) -> str:
    t = collector.alert_thresholds
    return "\n".join([
        "🔔 *Настройки алертов*",
        "",
        f"CPU:         `{t['cpu']}%`",
        f"RAM:         `{t['ram']}%`",
        f"Диск:        `{t['disk']}%`",
        f"Темп. CPU:   `{t['temp_cpu']}°C`",
        "",
        "Изменить: `/setalert cpu 80`",
        "Ключи: `cpu`, `ram`, `disk`, `temp_cpu`",
    ])


WELCOME = """
👋 *Мониторинг Linux системы*

Доступные команды:
/status — полный дашборд
/cpu — детально по CPU
/ram — детально по памяти
/disk — все диски
/temp — температуры
/net — сетевой трафик
/history — графики за час
/alerts — пороги алертов
/setalert [метрика] [порог] — изменить порог

Авто-алерты активны 🔔
""".strip()


# ─── Bot handlers ─────────────────────────────────────────────────────────────

def make_handlers(collector: MetricsCollector):
    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        collector.bot_chat_ids.add(update.effective_chat.id)
        await update.message.reply_text(WELCOME, parse_mode="Markdown")

    async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = collector.collect()
        await update.message.reply_text(_tg_status(m), parse_mode="Markdown")

    async def cpu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = collector.collect()
        await update.message.reply_text(_tg_cpu(m), parse_mode="Markdown")

    async def ram(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = collector.collect()
        await update.message.reply_text(_tg_ram(m), parse_mode="Markdown")

    async def disk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = collector.collect()
        await update.message.reply_text(_tg_disk(m), parse_mode="Markdown")

    async def temp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = collector.collect()
        await update.message.reply_text(_tg_temp(m), parse_mode="Markdown")

    async def net(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        m = collector.collect()
        await update.message.reply_text(_tg_net(m), parse_mode="Markdown")

    async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = _tg_history(collector)
        await update.message.reply_text(text, parse_mode="Markdown")

    async def alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(_tg_alerts(collector), parse_mode="Markdown")

    async def setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        args = ctx.args
        if not args or len(args) < 2:
            await update.message.reply_text(
                "Использование: `/setalert [метрика] [порог]`\n"
                "Метрики: `cpu`, `ram`, `disk`, `temp_cpu`",
                parse_mode="Markdown",
            )
            return
        key, val_str = args[0].lower(), args[1]
        if key not in collector.alert_thresholds:
            await update.message.reply_text(f"Неизвестная метрика: `{key}`", parse_mode="Markdown")
            return
        try:
            val = float(val_str)
        except ValueError:
            await update.message.reply_text("Порог должен быть числом.", parse_mode="Markdown")
            return
        collector.alert_thresholds[key] = val
        await update.message.reply_text(
            f"✅ Порог `{key}` установлен: `{val}`", parse_mode="Markdown"
        )

    return [
        CommandHandler("start", start),
        CommandHandler("status", status),
        CommandHandler("cpu", cpu),
        CommandHandler("ram", ram),
        CommandHandler("disk", disk),
        CommandHandler("temp", temp),
        CommandHandler("net", net),
        CommandHandler("history", history),
        CommandHandler("alerts", alerts),
        CommandHandler("setalert", setalert),
    ]


# ─── Background tasks ─────────────────────────────────────────────────────────

async def metrics_loop(collector: MetricsCollector, bot: Optional[Bot]):
    while True:
        try:
            m = collector.collect()
            alerts = collector.check_alerts(m)
            if alerts and bot and collector.bot_chat_ids:
                text = "⚠️ *Системный алерт*\n\n" + "\n".join(alerts)
                for chat_id in list(collector.bot_chat_ids):
                    try:
                        await bot.send_message(chat_id, text, parse_mode="Markdown")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[ошибка сбора метрик] {e}", file=sys.stderr)
        await asyncio.sleep(COLLECT_INTERVAL)


async def tui_loop(dashboard: Dashboard):
    console = Console()
    with Live(
        dashboard.make_layout(),
        console=console,
        refresh_per_second=1,
        screen=True,
    ) as live:
        while True:
            live.update(dashboard.make_layout())
            await asyncio.sleep(DISPLAY_INTERVAL)


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    if not TELEGRAM_TOKEN:
        print("ОШИБКА: переменная окружения TELEGRAM_TOKEN не установлена.", file=sys.stderr)
        print("Запустите: export TELEGRAM_TOKEN='ваш_токен'", file=sys.stderr)
        sys.exit(1)

    collector = MetricsCollector()
    dashboard = Dashboard(collector)

    # Initial collect so TUI has data immediately
    collector.collect()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for handler in make_handlers(collector):
        app.add_handler(handler)

    collector.bot = app.bot

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _shutdown(*_):
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)

        # Run metrics loop and TUI concurrently
        metrics_task = asyncio.create_task(metrics_loop(collector, app.bot))
        tui_task = asyncio.create_task(tui_loop(dashboard))

        await stop_event.wait()

        metrics_task.cancel()
        tui_task.cancel()
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
