"""Build labeled contact sheets from a folder of extracted pack images.
Usage: python libraries/contact_sheet.py <imgdir> <out_prefix> [--cols N] [--thumbw W] [--per-sheet N]
"""
import argparse
import glob
import os

from PIL import Image, ImageDraw

ap = argparse.ArgumentParser()
ap.add_argument("imgdir")
ap.add_argument("out_prefix")
ap.add_argument("--cols", type=int, default=4)
ap.add_argument("--thumbw", type=int, default=300)
ap.add_argument("--per-sheet", type=int, default=48)
args = ap.parse_args()

files = sorted(glob.glob(os.path.join(args.imgdir, "*.png")) +
               glob.glob(os.path.join(args.imgdir, "*.bmp")))
LABEL_H = 16
PAD = 4

sheets = [files[i:i + args.per_sheet] for i in range(0, len(files), args.per_sheet)]
for si, chunk in enumerate(sheets):
    thumbs = []
    for path in chunk:
        im = Image.open(path).convert("RGBA")
        scale = min(args.thumbw / im.width, 1.0)
        if scale < 1.0:
            im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
        bg = Image.new("RGBA", im.size, (96, 96, 96, 255))
        bg.alpha_composite(im)
        thumbs.append((os.path.basename(path), bg.convert("RGB")))
    cell_w = max(t.width for _, t in thumbs) + PAD * 2
    cell_h = max(t.height for _, t in thumbs) + LABEL_H + PAD * 2
    cols = args.cols
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    for i, (name, t) in enumerate(thumbs):
        x = (i % cols) * cell_w
        y = (i // cols) * cell_h
        sheet.paste(t, (x + PAD, y + PAD + LABEL_H))
        draw.text((x + PAD, y + 2), name, fill=(255, 255, 120))
    out = f"{args.out_prefix}_{si:02d}.png"
    sheet.save(out)
    print(out, f"({len(thumbs)} images)")
