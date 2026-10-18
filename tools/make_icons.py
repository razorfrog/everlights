"""Draw the app icons (a roofline of glowing lights) into static/. Needs Pillow."""
import math
import os

from PIL import Image, ImageDraw, ImageFilter

OUT = os.path.join(os.path.dirname(__file__), "..", "static")
COLORS = ["#FF8C1A", "#FFB547", "#B04BFF", "#FF8C1A", "#FFB547", "#B04BFF", "#FF8C1A"]


def draw(size, rounded):
    s = 4  # supersample
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    bg = ImageDraw.Draw(img)
    radius = int(n * 0.22) if rounded else 0
    bg.rounded_rectangle([0, 0, n - 1, n - 1], radius=radius, fill="#16141c")

    # Roofline: an inverted V of dots.
    peak, left, right = (n * 0.5, n * 0.28), (n * 0.14, n * 0.62), (n * 0.86, n * 0.62)
    pts = []
    k = 7
    for i in range(k):
        t = i / (k - 1)
        pts.append((left[0] + (peak[0] - left[0]) * t, left[1] + (peak[1] - left[1]) * t))
    for i in range(1, k):
        t = i / (k - 1)
        pts.append((peak[0] + (right[0] - peak[0]) * t, peak[1] + (right[1] - peak[1]) * t))
    r = n * 0.042

    glow = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    for i, (x, y) in enumerate(pts):
        c = COLORS[i % len(COLORS)]
        g.ellipse([x - r * 2.4, y - r * 2.4, x + r * 2.4, y + r * 2.4], fill=c + "70")
    glow = glow.filter(ImageFilter.GaussianBlur(n * 0.03))
    img.alpha_composite(glow)

    d = ImageDraw.Draw(img)
    for i, (x, y) in enumerate(pts):
        d.ellipse([x - r, y - r, x + r, y + r], fill=COLORS[i % len(COLORS)])
        d.ellipse([x - r * 0.45, y - r * 0.55, x + r * 0.15, y - r * 0.05], fill="#ffffff90")

    # House wall hint.
    w = n * 0.012
    d.line([(n * 0.24, n * 0.66), (n * 0.24, n * 0.84), (n * 0.76, n * 0.84), (n * 0.76, n * 0.66)],
           fill="#3a3646", width=int(w), joint="curve")
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    draw(180, rounded=False).convert("RGB").save(os.path.join(OUT, "apple-touch-icon.png"))
    draw(192, rounded=True).save(os.path.join(OUT, "icon-192.png"))
    draw(512, rounded=True).save(os.path.join(OUT, "icon-512.png"))
    draw(64, rounded=True).save(os.path.join(OUT, "favicon.png"))
    print("icons written")
