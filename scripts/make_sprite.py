#!/usr/bin/env python3
"""Turn a generated image into a clean, game-ready sprite sheet.

The image model is asked for a 256x64 four-frame strip and instead returns
something like 1983x793 with the frames wherever they landed. This tool does the
boring part: find the frames by looking for columns that contain any opaque
pixels, crop each one, scale it down with nearest-neighbour so the pixel edges
stay hard, and lay them out on an exact grid.

    python3 scripts/make_sprite.py in.png out.png --frames 4 --cell 64

Pillow only — matching the project's no-heavy-dependencies rule (and ImageMagick
is not installed on the author's machine).
"""

from __future__ import annotations

import argparse
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    sys.exit("this tool needs Pillow:  pip install --user Pillow")

#: Alpha at or below this counts as empty when hunting for frame gutters.
ALPHA_FLOOR = 8


def column_has_content(alpha, x: int, height: int) -> bool:
    for y in range(height):
        if alpha[x, y] > ALPHA_FLOOR:
            return True
    return False


def find_frame_spans(image: Image.Image, min_width: int = 8):
    """Column ranges that contain pixels, i.e. the individual frames."""
    alpha = image.split()[3].load()
    width, height = image.size
    spans, start = [], None
    for x in range(width):
        filled = column_has_content(alpha, x, height)
        if filled and start is None:
            start = x
        elif not filled and start is not None:
            if x - start >= min_width:
                spans.append((start, x))
            start = None
    if start is not None and width - start >= min_width:
        spans.append((start, width))
    return spans


def merge_to_count(spans, target: int):
    """Collapse the smallest gaps until exactly *target* frames remain.

    A character's arm can be separated from its body by a transparent column,
    which splits one frame in two. Repeatedly merging across the narrowest gap
    reassembles them without needing to understand the artwork.
    """
    spans = list(spans)
    while len(spans) > target:
        gaps = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
        _, index = min(gaps)
        spans[index] = (spans[index][0], spans[index + 1][1])
        del spans[index + 1]
    return spans


def cropped_frames(image: Image.Image, spans):
    """Tightly crop every frame, keeping them in order."""
    out = []
    for left, right in spans:
        strip = image.crop((left, 0, right, image.height))
        box = strip.getbbox()
        out.append(strip.crop(box) if box else strip)
    return out


def layout_frames(frames, cell: int, pad: int = 2):
    """Scale every frame by ONE factor, then bottom-centre each in its cell.

    Scaling frames independently is the obvious approach and it is wrong: the
    model draws some frames slightly smaller, and normalising each to fill its
    cell turns that into a character that visibly pulses as it walks. A single
    factor taken from the largest frame preserves the relative sizes the artwork
    actually has.
    """
    usable = cell - pad * 2
    widest = max(f.width for f in frames)
    tallest = max(f.height for f in frames)
    scale = min(usable / widest, usable / tallest)

    cells = []
    for frame in frames:
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        # NEAREST keeps the pixel-art edges hard; anything else turns it to mush.
        resized = frame.resize(size, Image.NEAREST)
        cell_image = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        cell_image.paste(resized, ((cell - size[0]) // 2, cell - pad - size[1]), resized)
        cells.append(cell_image)
    return cells


def build_sheet(source: str, target: str, frames: int, cell: int,
                verbose: bool = True) -> Image.Image:
    image = Image.open(source).convert("RGBA")
    spans = find_frame_spans(image)
    if verbose:
        print(f"  {source.split('/')[-1]}: {image.size[0]}x{image.size[1]}, "
              f"{len(spans)} span(s) found")

    if not spans:
        raise SystemExit(f"{source}: the image is entirely transparent")

    if len(spans) > frames:
        spans = merge_to_count(spans, frames)
    elif len(spans) < frames:
        # Fewer frames than asked for: repeat the last so the sheet stays valid.
        if verbose:
            print(f"    only {len(spans)} frames — padding to {frames}")
        spans = spans + [spans[-1]] * (frames - len(spans))

    cells = layout_frames(cropped_frames(image, spans), cell)
    sheet = Image.new("RGBA", (cell * frames, cell), (0, 0, 0, 0))
    for index, cell_image in enumerate(cells):
        sheet.paste(cell_image, (index * cell, 0))

    sheet.save(target)
    if verbose:
        print(f"    -> {target} ({sheet.size[0]}x{sheet.size[1]})")
    return sheet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--cell", type=int, default=64)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    build_sheet(args.source, args.target, args.frames, args.cell,
                verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
