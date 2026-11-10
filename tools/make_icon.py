"""Generate the application icon (``peverything/assets/peverything.ico``).

Run from the repository root::

    python tools/make_icon.py

The picture is a white magnifier on a blue rounded square, rendered at 512 px
and downsampled into the usual Windows icon sizes.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
TOP = (58, 140, 235)
BOTTOM = (12, 68, 152)
ASSET = Path(__file__).resolve().parent.parent / "peverything" / "assets" / "peverything.ico"


def gradient(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(image)
    for y in range(size):
        ratio = y / max(1, size - 1)
        colour = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=colour)
    return image


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def build() -> Image.Image:
    base = gradient(SIZE).convert("RGBA")
    base.putalpha(rounded_mask(SIZE, int(SIZE * 0.22)))
    draw = ImageDraw.Draw(base)

    # soft highlight in the upper left corner
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [-SIZE * 0.35, -SIZE * 0.55, SIZE * 0.75, SIZE * 0.45], fill=(255, 255, 255, 38)
    )
    base = Image.alpha_composite(base, Image.composite(glow, Image.new("RGBA", (SIZE, SIZE)), base.getchannel("A")))

    draw = ImageDraw.Draw(base)
    centre = (SIZE * 0.43, SIZE * 0.43)
    radius = SIZE * 0.24
    ring = int(SIZE * 0.075)
    draw.ellipse(
        [centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius],
        outline=(255, 255, 255, 255),
        width=ring,
    )
    # handle: thick round-capped diagonal
    start = (centre[0] + radius * 0.72, centre[1] + radius * 0.72)
    end = (SIZE * 0.79, SIZE * 0.79)
    draw.line([start, end], fill=(255, 255, 255, 255), width=int(SIZE * 0.10))
    cap = int(SIZE * 0.05)
    for point in (start, end):
        draw.ellipse(
            [point[0] - cap, point[1] - cap, point[0] + cap, point[1] + cap],
            fill=(255, 255, 255, 255),
        )
    return base


def main() -> int:
    image = build()
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        ASSET,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    preview = ASSET.with_suffix(".png")
    image.resize((256, 256), Image.LANCZOS).save(preview)
    print(f"wrote {ASSET} ({ASSET.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
