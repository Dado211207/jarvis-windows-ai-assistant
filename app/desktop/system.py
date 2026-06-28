"""System status tool — CPU, RAM, disk, battery, OS info via psutil."""

import platform
import socket
import getpass
from typing import Any, Dict, Optional

from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.system")


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


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="system_status",
            description="Display CPU, RAM, disk, battery, and OS information.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.SYSTEM,
        ),
        get_system_status,
    )
