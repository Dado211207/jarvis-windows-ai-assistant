"""System status tool — CPU, RAM, disk, battery, OS info via psutil."""

import platform
import socket
import getpass
from typing import Any, Dict, Optional

from app.core.models import PermissionLevel, RiskLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.system")

_NO_VERIFICATION = "Read-only query — nothing is changed, so there is no effect to verify beyond a successful return."


def get_system_status() -> dict:
    """Return a snapshot of the current system resource usage."""
    try:
        import psutil
    except ImportError:
        return {
            "success": False,
            "message": "psutil is not installed. Run: pip install psutil",
            "data": None,
        }

    cpu_percent = psutil.cpu_percent(interval=0.5)

    vm = psutil.virtual_memory()
    ram = {
        "used_gb": round(vm.used / 1024 ** 3, 2),
        "total_gb": round(vm.total / 1024 ** 3, 2),
        "percent": vm.percent,
    }

    disk = psutil.disk_usage("/")
    disk_info = {
        "used_gb": round(disk.used / 1024 ** 3, 2),
        "total_gb": round(disk.total / 1024 ** 3, 2),
        "percent": disk.percent,
    }

    battery: Optional[Dict[str, Any]] = None
    try:
        batt = psutil.sensors_battery()
        if batt:
            battery = {
                "percent": round(batt.percent, 1),
                "plugged_in": batt.power_plugged,
                "seconds_left": batt.secsleft if batt.secsleft != psutil.POWER_TIME_UNLIMITED else None,
            }
    except (AttributeError, NotImplementedError):
        pass

    data = {
        "cpu_percent": cpu_percent,
        "ram": ram,
        "disk": disk_info,
        "battery": battery,
        "os": platform.system(),
        "os_version": platform.version(),
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
    }

    lines = [
        "=== System Status ===",
        f"  OS        : {data['os']} {data['os_version'][:40]}",
        f"  Host      : {data['hostname']}  User: {data['username']}",
        f"  CPU       : {cpu_percent}%",
        f"  RAM       : {ram['used_gb']} / {ram['total_gb']} GB  ({ram['percent']}%)",
        f"  Disk      : {disk_info['used_gb']} / {disk_info['total_gb']} GB  ({disk_info['percent']}%)",
    ]
    if battery:
        plug = "plugged in" if battery["plugged_in"] else "on battery"
        lines.append(f"  Battery   : {battery['percent']}%  ({plug})")
    else:
        lines.append("  Battery   : not available")

    return {"success": True, "message": "\n".join(lines), "data": data}


def get_disk_space() -> dict:
    """Return read-only disk usage for the primary disk."""
    try:
        import psutil
    except ImportError:
        return {"success": False, "message": "psutil is not installed.", "data": None}

    disk = psutil.disk_usage("/")
    free_gb = round(disk.free / 1024 ** 3, 2)
    used_gb = round(disk.used / 1024 ** 3, 2)
    total_gb = round(disk.total / 1024 ** 3, 2)
    data = {
        "free_gb": free_gb,
        "used_gb": used_gb,
        "total_gb": total_gb,
        "percent_used": disk.percent,
    }
    msg = (
        f"Disk: {used_gb} GB used / {total_gb} GB total "
        f"({disk.percent}% used) — {free_gb} GB free"
    )
    return {"success": True, "message": msg, "data": data}


def get_network_info() -> dict:
    """Return safe local network info. No scanning, no external requests."""
    try:
        import socket
        hostname = socket.gethostname()
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            local_ip = "unavailable"
        data = {"hostname": hostname, "local_ip": local_ip}
        msg = f"Network: hostname={hostname}  local IP={local_ip}"
        return {"success": True, "message": msg, "data": data}
    except Exception as exc:
        return {"success": False, "message": f"Failed to retrieve network info: {exc}", "data": None}


def get_battery_status() -> dict:
    """Return battery / power status. Returns gracefully if unavailable."""
    try:
        import psutil
        batt = psutil.sensors_battery()
        if batt is None:
            return {
                "success": True,
                "message": "Battery information not available on this system.",
                "data": {"available": False},
            }
        plug = "plugged in" if batt.power_plugged else "on battery"
        data = {
            "available": True,
            "percent": round(batt.percent, 1),
            "plugged_in": batt.power_plugged,
        }
        msg = f"Battery: {data['percent']}% ({plug})"
        return {"success": True, "message": msg, "data": data}
    except (AttributeError, NotImplementedError):
        return {
            "success": True,
            "message": "Battery information not available on this system.",
            "data": {"available": False},
        }
    except ImportError:
        return {"success": False, "message": "psutil is not installed.", "data": None}


def get_current_time() -> dict:
    """The local date and time, from this computer's own clock.

    Trivial, and worth having: it is the single most common thing anyone
    asks an assistant, and without a tool for it the question falls
    through to the AI — which does not know what time it is, will guess,
    and will be wrong. A deterministic route beats a confident invention.
    """
    from datetime import datetime

    now = datetime.now().astimezone()
    data = {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%A, %d %B %Y"),
        "time": now.strftime("%H:%M"),
        "timezone": now.strftime("%Z") or "local time",
    }
    return {
        "success": True,
        "message": f"It is {data['time']} on {data['date']} ({data['timezone']}).",
        "data": data,
    }


# Enough to answer "what is slowing my computer down" without turning
# into a process browser.
TOP_PROCESS_COUNT = 5


def get_top_processes() -> dict:
    """The few processes using the most memory right now.

    Memory rather than CPU: a single psutil CPU reading per process is
    measured since that process's own last call, which for a first look
    is effectively noise. Memory is a real number the first time it is
    asked for, so this reports something true rather than something
    impressive-looking.

    This is a snapshot of the user's own machine, taken when they ask.
    Nothing is sampled in the background, recorded, or stored — that
    would be the monitoring CLAUDE.md rules out.
    """
    try:
        import psutil
    except ImportError:
        return {"success": False, "message": "psutil is not installed.", "data": None}

    processes = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            info = proc.info
            memory = info.get("memory_info")
            if not info.get("name") or memory is None:
                continue
            processes.append({"name": info["name"], "memory_mb": round(memory.rss / 1024 ** 2, 1)})
        except Exception:  # noqa: BLE001 — a process that exited mid-iteration
            continue

    if not processes:
        return {"success": True, "message": "No process information is available.", "data": {"processes": []}}

    processes.sort(key=lambda p: p["memory_mb"], reverse=True)
    top = processes[:TOP_PROCESS_COUNT]
    lines = [f"Top {len(top)} processes by memory:"]
    lines += [f"  {p['name']}  —  {p['memory_mb']} MB" for p in top]

    return {"success": True, "message": "\n".join(lines), "data": {"processes": top}}


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="current_time",
            description="Show the current local date and time from this computer's clock.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.READ_ONLY,
            verification_strategy=_NO_VERIFICATION,
        ),
        get_current_time,
    )
    registry.register(
        ToolDefinition(
            name="top_processes",
            description=(
                "Show the few processes using the most memory right now. A snapshot "
                "taken on request — nothing is monitored or recorded in the background."
            ),
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.READ_ONLY,
            verification_strategy=_NO_VERIFICATION,
        ),
        get_top_processes,
    )
    registry.register(
        ToolDefinition(
            name="system_status",
            description="Display CPU, RAM, disk, battery, and OS information.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.READ_ONLY,
            verification_strategy=_NO_VERIFICATION,
        ),
        get_system_status,
    )
    registry.register(
        ToolDefinition(
            name="disk_space",
            description="Show disk usage: used, free, and total space.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.READ_ONLY,
            verification_strategy=_NO_VERIFICATION,
        ),
        get_disk_space,
    )
    registry.register(
        ToolDefinition(
            name="network_info",
            description="Show local network info (hostname, local IP). No scanning or external requests.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.READ_ONLY,
            verification_strategy=_NO_VERIFICATION,
        ),
        get_network_info,
    )
    registry.register(
        ToolDefinition(
            name="battery_status",
            description="Show battery / power status. Returns gracefully if unavailable.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
            risk=RiskLevel.READ_ONLY,
            verification_strategy=_NO_VERIFICATION,
        ),
        get_battery_status,
    )
