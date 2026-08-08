"""Generates JARVIS's application icon from the dashboard's own brand
tokens (app/ui/static/style.css's --bg and --accent) — not a design
tool invocation, not third-party artwork. Deliberately simple: a solid
dark badge with a bold single-color "J" glyph, matching the sidebar's
own <div class="sidebar-logo">J</div> mark, is what stays legible at
16x16 — anything with fine detail or a second color washes out at that
size. No Marvel/Iron Man/movie references; this is original.

Run once, at development time, to (re)produce the committed assets:

    python scripts/generate_icon.py

Writes app/ui/static/icon.ico (multi-resolution: 16/24/32/48/128/256)
and app/ui/static/icon.png (256x256, single-size — used wherever a PNG
is more convenient than an ICO, e.g. a future web favicon). Both are
committed to the repo as static assets, the same as style.css or
app.js — this script is a dev-time tool, not something the build or
the app itself ever runs.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# From app/ui/static/style.css's :root block — kept in sync manually
# since this is a one-off generation script, not a build-time consumer
# of the CSS file.
BG = "#080c14"       # --bg
ACCENT = "#00d4ff"   # --accent
GLYPH = "#f8fafc"    # near-white, for contrast against BG at small sizes

CANVAS_SIZE = 256
ICO_SIZES = [16, 24, 32, 48, 128, 256]

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "app" / "ui" / "static"


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError(
        "No bold sans-serif TTF found among: " + ", ".join(FONT_CANDIDATES) +
        " — install one (e.g. fonts-dejavu-core) or add its path to FONT_CANDIDATES."
    )


def build_icon() -> Image.Image:
    size = CANVAS_SIZE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = size * 0.06
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=size * 0.22,
        fill=BG,
        outline=ACCENT,
        width=max(2, round(size * 0.02)),
    )

    font = _load_font(round(size * 0.62))
    text = "J"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = ((size - text_w) / 2 - bbox[0], (size - text_h) / 2 - bbox[1])
    draw.text(position, text, font=font, fill=GLYPH)

    return image


def main() -> None:
    icon = build_icon()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ico_path = OUTPUT_DIR / "icon.ico"
    icon.save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"Wrote {ico_path} ({', '.join(f'{s}x{s}' for s in ICO_SIZES)})")

    png_path = OUTPUT_DIR / "icon.png"
    icon.save(png_path, format="PNG")
    print(f"Wrote {png_path} ({CANVAS_SIZE}x{CANVAS_SIZE})")


if __name__ == "__main__":
    main()
