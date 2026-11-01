"""Layouts and printing.

All prints are 4x6 landscape at the size set in config.json
(default 1844x1240, the DNP DS-RX1 / DS620 native 4x6 resolution).
"""

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

LAYOUTS = {"single": 1, "grid": 4}


def _open(path):
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def _rounded_mask(size, radius):
    """Alpha mask with rounded corners, drawn 4x larger and scaled down for smooth edges."""
    if not radius:
        return None
    from PIL import ImageDraw
    w, h = size
    big = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius=radius * 4, fill=255)
    return big.resize(size, Image.LANCZOS)


def print_size(cfg):
    """Print pixels (W, H). A camera on its side (rotate 90/270) means a portrait 4x6."""
    W, H = cfg["print_width"], cfg["print_height"]
    return (H, W) if int(cfg.get("rotate", 0)) % 180 else (W, H)


def grid_cell(cfg):
    """Size of one photo cell in the 4-up, and the border/gap sizes around it."""
    W, H = print_size(cfg)
    g = cfg.get("spacing", 24)
    m = g + cfg.get("print_bleed", 28)
    return (W - 2 * m - g) // 2, (H - 2 * m - g) // 2, m, g


def crop_to_cell(img, cfg):
    """Center-crop a photo to the 4-up cell's shape, so the singles, the GIF and the
    4-up all show exactly the same framing."""
    cw, ch, _, _ = grid_cell(cfg)
    target = cw / ch
    w, h = img.size
    if abs(w / h - target) < 0.002:
        return img
    if w / h > target:
        nw = int(round(h * target)); x = (w - nw) // 2
        return img.crop((x, 0, x + nw, h))
    nh = int(round(w / target)); y = (h - nh) // 2
    return img.crop((0, y, w, y + nh))


def trim_bleed(print_img, cfg):
    """The 4-up without the extra outer border the printer trims off: what the print
    looks like in hand, and what guests see and save."""
    b = cfg.get("print_bleed", 28)
    w, h = print_img.size
    return print_img.crop((b, b, w - b, h - b)) if b > 0 else print_img


def compose(photo_paths, layout, cfg) -> Image.Image:
    W, H = print_size(cfg)
    canvas = Image.new("RGB", (W, H), cfg.get("background", "#ffffff"))

    if layout == "single":
        canvas.paste(ImageOps.fit(_open(photo_paths[0]), (W, H), Image.LANCZOS), (0, 0))
        return canvas

    if layout == "grid":
        # Same spacing between photos and around the outside. The printer trims the
        # outer edge of a 4x6, so the outer border gets extra (print_bleed) to survive it.
        cw, ch, m, g = grid_cell(cfg)
        # center the grid if integer rounding leaves a pixel or two
        ox = (W - (2 * cw + g)) // 2
        oy = (H - (2 * ch + g)) // 2
        mask = _rounded_mask((cw, ch), cfg.get("corner_radius", 0))
        for i, p in enumerate(photo_paths[:4]):
            cell = ImageOps.fit(_open(p), (cw, ch), Image.LANCZOS)
            canvas.paste(cell, (ox + (i % 2) * (cw + g), oy + (i // 2) * (ch + g)), mask)
        return canvas

    raise ValueError(f"Unknown layout {layout}")


def list_printers():
    if sys.platform == "win32":
        try:
            import win32print
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            return [p[2] for p in win32print.EnumPrinters(flags)]
        except Exception:
            return []
    try:
        out = subprocess.run(["lpstat", "-p"], capture_output=True, text=True, timeout=5).stdout
        return [line.split()[1] for line in out.splitlines() if line.startswith("printer ")]
    except Exception:
        return []


def _print_windows(path, copies, printer):
    """Draw the image edge to edge on the driver's full physical page.

    Paper size comes from the printer's defaults, so set the DNP driver to
    4x6 once in Windows printer preferences. The image is rotated to match
    whatever orientation the driver reports.
    """
    import win32print
    import win32ui
    from PIL import ImageWin

    if printer not in [p[2] for p in win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]:
        raise RuntimeError(f'Printer "{printer}" not found. Check the name in config.json.')

    PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY = 110, 111, 112, 113
    img = Image.open(path).convert("RGB")
    dc = win32ui.CreateDC()
    dc.CreatePrinterDC(printer)
    try:
        pw, ph = dc.GetDeviceCaps(PHYSICALWIDTH), dc.GetDeviceCaps(PHYSICALHEIGHT)
        ox, oy = dc.GetDeviceCaps(PHYSICALOFFSETX), dc.GetDeviceCaps(PHYSICALOFFSETY)
        if (pw > ph) != (img.width > img.height):
            img = img.rotate(90, expand=True)
        dib = ImageWin.Dib(img)
        dc.StartDoc(f"Photobooth {Path(path).parent.name}")
        for _ in range(copies):
            dc.StartPage()
            dib.draw(dc.GetHandleOutput(), (-ox, -oy, pw - ox, ph - oy))
            dc.EndPage()
        dc.EndDoc()
    finally:
        dc.DeleteDC()


def print_image(path, copies, cfg):
    printer = cfg.get("printer", "").strip()
    copies = max(1, min(int(copies), int(cfg.get("max_copies", 6))))
    if not printer:
        return {"ok": True, "skipped": True,
                "message": "No printer set in config.json. Saved without printing."}
    if sys.platform == "win32":
        try:
            _print_windows(path, copies, printer)
        except Exception as e:
            return {"ok": False, "message": f"{type(e).__name__}: {e}"[:300]}
        return {"ok": True, "message": "", "copies": copies}

    cmd =[part.format(printer=printer, copies=copies, file=str(path))
           for part in cfg["print_command"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()[:300]
        return {"ok": False, "message": msg or f"Print command exited with code {r.returncode}"}
    return {"ok": True, "message": r.stdout.strip(), "copies": copies}
