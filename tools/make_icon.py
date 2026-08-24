"""Generate the connector tiles: a white RSS mark on an Inoreader-blue rounded square.

Deliberately the standard RSS glyph rather than Inoreader's own wordmark -- it
reads correctly at 150px in the Content Hub grid and carries no brand asset.
Drawn at 8x and downsampled, because Pillow's arcs alias badly at final size.
"""
import sys
from PIL import Image, ImageDraw

BLUE = (24, 117, 240, 255)   # Inoreader's brand blue
WHITE = (255, 255, 255, 255)


def render(size, path):
    s = size * 8
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BLUE)

    # RSS mark: a dot at the origin plus two concentric arcs. The origin is offset
    # so the glyph's bounding box -- not the dot -- lands in the middle of the tile.
    ox, oy = int(s * 0.34), int(s * 0.66)
    dot_r = int(s * 0.062)
    d.ellipse([ox - dot_r, oy - dot_r, ox + dot_r, oy + dot_r], fill=WHITE)

    for radius, width in ((s * 0.20, s * 0.075), (s * 0.34, s * 0.075)):
        d.arc([ox - radius, oy - radius, ox + radius, oy + radius],
              start=270, end=360, fill=WHITE, width=int(width))

    img.resize((size, size), Image.LANCZOS).save(path, "PNG")
    print(f"wrote {path} ({size}x{size})")


render(150, sys.argv[1])
render(500, sys.argv[2])
