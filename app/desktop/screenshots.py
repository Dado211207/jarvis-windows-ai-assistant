"""Screenshot tool — captures and saves screenshots to data/screenshots/."""

from datetime import datetime
from pathlib import Path

from app.config import settings
from app.core.models import PermissionLevel, ToolCategory, ToolDefinition
from app.logging_config import get_logger

logger = get_logger("desktop.screenshots")


def take_screenshot() -> dict:
    """Capture the primary screen and save to data/screenshots/."""
    try:
        from PIL import ImageGrab
    except ImportError:
        return {
            "success": False,
            "message": "Pillow is not installed. Run: pip install pillow",
            "data": None,
        }

    save_dir: Path = settings.screenshots_dir
    save_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{timestamp}.png"
    filepath = save_dir / filename

    try:
        img = ImageGrab.grab()
        img.save(str(filepath))
        logger.info("Screenshot saved: %s", filepath)
        return {
            "success": True,
            "message": f"Screenshot saved to {filepath}",
            "data": {"path": str(filepath)},
        }
    except OSError as exc:
        # ImageGrab.grab() raises OSError on headless Linux/CI without a display
        logger.warning("Screenshot failed (likely headless environment): %s", exc)
        return {
            "success": False,
            "message": (
                f"Could not capture screenshot: {exc}. "
                "A display is required (Windows or X11)."
            ),
            "data": None,
        }
    except Exception as exc:
        logger.error("Screenshot error: %s", exc, exc_info=True)
        return {"success": False, "message": f"Screenshot failed: {exc}", "data": None}


def register_tools(registry) -> None:
    registry.register(
        ToolDefinition(
            name="take_screenshot",
            description="Capture a screenshot and save it to data/screenshots/.",
            permission_level=PermissionLevel.SAFE,
            category=ToolCategory.UTILITY,
        ),
        take_screenshot,
    )
