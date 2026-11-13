"""In-memory synthetic sprite sheets (Pillow only) shared by the sprite/composition tests."""
import io

from PIL import Image, ImageDraw

GREEN = (37, 245, 19)
PAPER = (245, 238, 220)


def _jpeg(img: Image.Image, quality: int = 85) -> Image.Image:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def grid_lines(size, cols, rows, gutters: bool, gutter: int = 12, margin: int | None = None):
    """Known cut positions (x list, y list) of walk_sheet/jump_sheet: gutter centres, or ideal cuts."""
    w, h = size
    if not gutters:
        return ([round(k * w / cols) for k in range(1, cols)],
                [round(k * h / rows) for k in range(1, rows)])
    m = gutter if margin is None else margin
    cw, ch = (w - 2 * m - (cols - 1) * gutter) / cols, (h - 2 * m - (rows - 1) * gutter) / rows
    return ([round(m + k * cw + (k - 1) * gutter + gutter / 2) for k in range(1, cols)],
            [round(m + k * ch + (k - 1) * gutter + gutter / 2) for k in range(1, rows)])


def _cells(size, cols, rows, gutters, gutter, margin):
    w, h = size
    if not gutters:
        return [(round(c * w / cols), round(r * h / rows), round((c + 1) * w / cols), round((r + 1) * h / rows))
                for r in range(rows) for c in range(cols)]
    cw, ch = (w - 2 * margin - (cols - 1) * gutter) / cols, (h - 2 * margin - (rows - 1) * gutter) / rows
    return [(round(margin + c * (cw + gutter)), round(margin + r * (ch + gutter)),
             round(margin + c * (cw + gutter) + cw), round(margin + r * (ch + gutter) + ch))
            for r in range(rows) for c in range(cols)]


def _character(img: Image.Image, cx: int, base_y: int, scale: float = 1.0) -> None:
    """Outlined body + semi-transparent glass disc (alpha 0.5 over the background) + 1-px antenna."""
    dr = ImageDraw.Draw(img)
    bw, bh, hr = int(44 * scale), int(80 * scale), int(30 * scale)
    top = base_y - bh
    dr.ellipse((cx - bw, top, cx + bw, base_y), fill=(250, 250, 250), outline=(20, 30, 60), width=3)
    head_cy = top - hr // 2
    dr.ellipse((cx - hr, head_cy - hr, cx + hr, head_cy + hr), fill=(240, 240, 245), outline=(20, 30, 60), width=3)
    layer = Image.new("RGB", img.size, (90, 120, 200))
    mask = Image.new("L", img.size, 0)
    gr = int(hr * 0.7)
    ImageDraw.Draw(mask).ellipse((cx - gr, head_cy - gr, cx + gr, head_cy + gr), fill=128)
    img.paste(layer, (0, 0), mask)
    dr.line((cx, head_cy - hr, cx, head_cy - hr - int(22 * scale)), fill=(20, 30, 60), width=1)


def walk_sheet(gutters: bool, size=(1376, 768), cols=4, rows=2) -> Image.Image:
    gutter, margin = 12, 12
    img = Image.new("RGB", size, (255, 255, 255) if gutters else GREEN)
    dr = ImageDraw.Draw(img)
    cells = _cells(size, cols, rows, gutters, gutter, margin)
    if gutters:
        for c in cells:
            dr.rectangle((c[0], c[1], c[2] - 1, c[3] - 1), fill=GREEN)
    xoff = [-10, 6, 10, -4, 0, 8, -8, 4]
    bob = [0, -3, -6, -3, 0, -3, -6, -3]
    for k, (x0, y0, x1, y1) in enumerate(cells):
        cx = (x0 + x1) // 2 + xoff[k % len(xoff)]
        base = (y0 + y1) // 2 + 66 + bob[k % len(bob)]
        _character(img, cx, base)
    return _jpeg(img)


def jump_sheet() -> Image.Image:
    size, cols, rows, gutter = (1024, 1024), 3, 3, 16
    img = Image.new("RGB", size, (255, 255, 255))
    dr = ImageDraw.Draw(img)
    cells = _cells(size, cols, rows, True, gutter, gutter)
    for c in cells:
        dr.rectangle((c[0], c[1], c[2] - 1, c[3] - 1), fill=GREEN)
    # ground offsets: crouch, rise, apex, fall, land (smooth across the row changes)
    lift = [0, -30, -70, -85, -60, -30, 0, 5, 0]
    for k, (x0, y0, x1, y1) in enumerate(cells):
        _character(img, (x0 + x1) // 2 + (k % 3 - 1) * 4, y1 - 80 + lift[k])
    return _jpeg(img)


def white_char_sheet(cols=4, rows=2, cell=200) -> Image.Image:
    img = Image.new("RGB", (cols * cell, rows * cell), (0, 255, 0))
    dr = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            cx, cy = c * cell + cell // 2, r * cell + cell // 2
            dr.ellipse((cx - 35, cy - 45, cx + 35, cy + 45), fill=(255, 255, 255))
    return img


def composition_set(size=(800, 600)):
    """(master, plate, piece_green, piece_bbox): paper + two shops + a 'hermes' figure."""
    def hermes(dr, fill, outline=None):
        dr.ellipse((470, 330, 520, 380), fill=fill, outline=outline)             # head
        dr.rectangle((480, 380, 510, 470), fill=fill, outline=outline)           # body
        dr.polygon([(480, 400), (430, 360), (440, 420)], fill=fill, outline=outline)  # wing
        dr.polygon([(510, 400), (560, 360), (550, 420)], fill=fill, outline=outline)  # wing
    master = Image.new("RGB", size, PAPER)
    dr = ImageDraw.Draw(master)
    dr.rectangle((60, 120, 260, 300), fill=(180, 90, 60), outline=(60, 30, 20), width=3)     # shop 1
    dr.rectangle((560, 140, 740, 300), fill=(70, 110, 160), outline=(20, 30, 60), width=3)   # shop 2
    hermes(dr, (210, 170, 40), (80, 60, 10))
    plate = master.copy()
    hermes(ImageDraw.Draw(plate), PAPER, PAPER)
    piece = Image.new("RGB", size, GREEN)
    hermes(ImageDraw.Draw(piece), (210, 170, 40), (80, 60, 10))
    mask = Image.new("L", size, 0)
    hermes(ImageDraw.Draw(mask), 255, 255)
    return master, plate, piece, mask.getbbox()
