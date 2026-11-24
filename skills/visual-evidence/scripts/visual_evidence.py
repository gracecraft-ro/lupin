#!/usr/bin/env python3
"""Add captions and layouts to visual evidence. Needs ffmpeg and ffprobe.

Run it through Nix so both tools are on PATH:

  nix shell nixpkgs#ffmpeg -c python3 scripts/visual_evidence.py pair \
    before.png after.png --caption "Save button moves to the bottom bar" --out pair.png

  nix shell nixpkgs#ffmpeg -c python3 scripts/visual_evidence.py gif \
    demo.mp4 --caption "Tap Save to confirm" --out demo.gif

  nix shell nixpkgs#ffmpeg -c python3 scripts/visual_evidence.py sheet \
    top.png="Top" front.png="Front" --title "Part A, same views" --out sheet.png

  nix shell nixpkgs#ffmpeg nixpkgs#nodejs -c python3 scripts/visual_evidence.py stl \
    part-after.stl --before part-before.stl --title "Part, before and after" --out part.png

Each command prints one line: wrote PATH WIDTHxHEIGHT bytes=N
Open the file and check it. The script does not judge the picture.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

FONT = "DejaVu Sans"  # fontconfig name; --font takes a .ttf/.ttc path instead
PAD = 8  # px between a band edge and its text
LINE_GAP = 6  # px between caption lines
MAX_LINES = 4  # a caption or title longer than this is too long to read at this size
LABEL_H = 40  # px band above each pair panel
LABEL_SIZE = 24
TITLE_SIZE = 24
TILE_LABEL_SIZE = 18
RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_stl.mjs")
STL_SIZE = 800  # px per rendered view, before tiling
STL_VIEWS = [("iso", "ISO"), ("plus-x", "+X"), ("minus-x", "-X"), ("plus-y", "+Y"),
             ("minus-y", "-Y"), ("plus-z", "+Z"), ("minus-z", "-Z")]


def die(message):
    sys.exit(f"visual_evidence: {message}")


def need(tool):
    if shutil.which(tool) is None:
        die(f"{tool} not found. Run through: nix shell nixpkgs#ffmpeg -c python3 ...")


def checked_input(path):
    if not os.path.isfile(path):
        die(f"input not found: {path}")
    return os.path.abspath(path)


def checked_out(path, ext):
    if os.path.splitext(path)[1].lower() != ext:
        die(f"--out must end in {ext} for this command")
    return os.path.abspath(path)


def esc(value):
    """Escape a value for a filtergraph option."""
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def font_option(font):
    if os.path.isfile(font):
        return f"fontfile={esc(font)}"
    return f"font={esc(font)}"


def probe(path):
    """Return (width, height) of the first video stream."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        die(f"cannot read an image or video from {path}")
    width, height = result.stdout.strip().split(",")[:2]
    return int(width), int(height)


def ink_width(text, font, size, folder):
    """Return the width in px of the ink that drawtext draws for text."""
    if not text:
        return 0
    width = len(text) * size * 2 + 2 * PAD  # wider than any line of this text
    width += width % 2  # yuv sources need an even width
    height = 2 * size
    name = write_text(folder, "measure.txt", text)
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=white:s={width}x{height},format=gray",
         "-vf", caption_draw(font, name, size, PAD, PAD),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        cwd=folder, capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) != width * height:
        die("ffmpeg failed while measuring text:\n" + result.stderr.decode(errors="replace").strip())
    rows = [result.stdout[i:i + width] for i in range(0, width * height, width)]
    ink_right = max(len(row.rstrip(b"\xff")) for row in rows)  # 0xff is white
    return max(0, ink_right - PAD)


def longest_fit(word, fits):
    """Return the longest prefix of word that fits. word itself must not fit."""
    low, high = 1, len(word) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if fits(word[:mid]):
            low = mid
        else:
            high = mid - 1
    return word[:low]


def wrap(text, width_px, font, size, folder):
    """Break text into lines that fit width_px, measured with the font that draws them.

    A glyph-width guess clips wide letters. A word wider than a line is cut.
    """
    def fits(line):
        return ink_width(line, font, size, folder) <= width_px

    lines = []

    def emit(line):
        lines.append(line)
        if len(lines) > MAX_LINES:
            die(f"text needs more than {MAX_LINES} lines at this width. Shorten it.")

    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if fits(candidate):
                line = candidate
                continue
            if line:
                emit(line)
            line = word
            while not fits(line):
                cut = longest_fit(line, fits)
                emit(cut)
                line = line[len(cut):]
        emit(line)
    return lines


def band_height(line_count, size):
    return 2 * PAD + line_count * (size + LINE_GAP)


def write_text(folder, name, text):
    """ffmpeg reads text from a file. This avoids escaping the caption."""
    with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    return name


def caption_draw(font, name, size, x, y):
    return (f"drawtext={font_option(font)}:textfile={name}:x={x}:y={y}:"
            f"fontsize={size}:fontcolor=black:line_spacing={LINE_GAP}")


def output_args(chain, out, fps):
    """Return the ffmpeg arguments that write the filtergraph to out."""
    if out.lower().endswith(".gif"):
        # -r, not the fps filter: the fps filter drops a lone still frame.
        graph = (f"{chain},split[s0][s1];"
                 "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer")
        return ["-filter_complex", graph, "-r", str(fps), out]
    return ["-filter_complex", chain, "-frames:v", "1", out]


def run_ffmpeg(inputs, out_args, folder):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, *out_args]
    result = subprocess.run(cmd, cwd=folder, capture_output=True, text=True)
    if result.returncode != 0:
        die("ffmpeg failed:\n" + result.stderr.strip())


def report(out):
    width, height = probe(out)
    print(f"wrote {out} {width}x{height} bytes={os.path.getsize(out)}")


def cmd_pair(args, folder):
    before = checked_input(args.before)
    after = checked_input(args.after)
    out = checked_out(args.out, ".png")
    w1, h1 = probe(before)
    w2, h2 = probe(after)
    h = min(h1, h2)  # both panels share one height
    panel_w = round(w1 * h / h1) + round(w2 * h / h2)
    lines = wrap(args.caption, panel_w - 24, args.font, args.size, folder)
    band = band_height(len(lines), args.size)
    write_text(folder, "caption.txt", "\n".join(lines))
    chain = (
        f"[0:v]scale=-2:{h},pad=iw:ih+{LABEL_H}:0:{LABEL_H}:white,"
        f"drawtext=text=Before:x=12:y={PAD}:fontsize={LABEL_SIZE}:fontcolor=black[a];"
        f"[1:v]scale=-2:{h},pad=iw:ih+{LABEL_H}:0:{LABEL_H}:white,"
        f"drawtext=text=After:x=12:y={PAD}:fontsize={LABEL_SIZE}:fontcolor=black[b];"
        f"[a][b]hstack=inputs=2:shortest=1,pad=iw:ih+{band}:0:{band}:white,"
        + caption_draw(args.font, "caption.txt", args.size, 12, PAD)
    )
    run_ffmpeg(["-i", before, "-i", after], output_args(chain, out, args.fps), folder)
    report(out)


def cmd_gif(args, folder):
    src = checked_input(args.input)
    out = checked_out(args.out, ".gif")
    lines = wrap(args.caption, args.width - 16, args.font, args.size, folder)
    band = band_height(len(lines), args.size)
    write_text(folder, "caption.txt", "\n".join(lines))
    chain = (
        f"[0:v]scale={args.width}:-2:flags=lanczos,pad=iw:ih+{band}:0:0:white,"
        + caption_draw(args.font, "caption.txt", args.size, 8, f"h-{band - PAD}")
    )
    run_ffmpeg(["-i", src], output_args(chain, out, args.fps), folder)
    report(out)


def cmd_sheet(args, folder):
    if len(args.views) < 2:
        die("sheet needs at least two views")
    items = []
    for raw in args.views:
        path, _, label = raw.partition("=")
        items.append((checked_input(path), label or os.path.splitext(os.path.basename(path))[0]))
    make_sheet(items, args, checked_out(args.out, ".png"), folder)


def make_sheet(items, args, out, folder):
    tile_w = args.tile_width
    first_w, first_h = probe(items[0][0])
    tile_h = round(tile_w * first_h / first_w) // 2 * 2
    cols = min(args.cols, len(items))
    lines = wrap(args.title, cols * tile_w - 24, args.font, TITLE_SIZE, folder)
    band = band_height(len(lines), TITLE_SIZE)
    write_text(folder, "caption.txt", "\n".join(lines))
    chains = []
    for index, (_, label) in enumerate(items):
        name = write_text(folder, f"tile{index}.txt", label)
        chains.append(
            f"[{index}:v]scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease,"
            f"pad={tile_w}:{tile_h}:(ow-iw)/2:(oh-ih)/2:white,"
            f"drawtext={font_option(args.font)}:textfile={name}:x=6:y=6:"
            f"fontsize={TILE_LABEL_SIZE}:fontcolor=white:box=1:boxcolor=black@0.5[t{index}]"
        )
    layout = "|".join(
        f"{(index % cols) * tile_w}_{(index // cols) * tile_h}" for index in range(len(items))
    )
    tiles = "".join(f"[t{index}]" for index in range(len(items)))
    chains.append(
        f"{tiles}xstack=inputs={len(items)}:layout={layout}:fill=white,"
        f"pad=iw:ih+{band}:0:{band}:white,"
        + caption_draw(args.font, "caption.txt", TITLE_SIZE, 12, PAD)
    )
    inputs = [arg for path, _ in items for arg in ("-i", path)]
    run_ffmpeg(inputs, output_args(";".join(chains), out, args.fps), folder)
    report(out)


def render_views(stl, folder, frame=None):
    """Render the seven views of stl. Return (path, label) pairs in STL_VIEWS order.

    frame is another STL. Both files then share one scale and centre, so a size change shows.
    """
    os.makedirs(folder, exist_ok=True)
    cmd = ["node", RENDER, stl, folder, "--size", str(STL_SIZE)]
    if frame:
        cmd += ["--frame", frame]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die("render_stl failed:\n" + result.stderr.strip())
    stem = os.path.splitext(os.path.basename(stl))[0]
    return [(os.path.join(folder, f"{stem}-{name}.png"), label) for name, label in STL_VIEWS]


def cmd_stl(args, folder):
    need("node")
    new = checked_input(args.stl)
    out = checked_out(args.out, ".png")
    if args.before:
        before = checked_input(args.before)
        before_views = render_views(before, os.path.join(folder, "before"), frame=new)
        after_views = render_views(new, os.path.join(folder, "after"), frame=before)
        items = ([(path, f"BEFORE {label}") for path, label in before_views]
                 + [(path, f"AFTER {label}") for path, label in after_views])
    else:
        items = render_views(new, os.path.join(folder, "new"))
    # One row for one part. Two rows, before then after, when --before is given.
    sheet = argparse.Namespace(title=args.title, cols=7, tile_width=args.tile_width,
                               font=args.font, fps=args.fps)
    make_sheet(items, sheet, out, folder)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--font", default=FONT,
                        help="fontconfig name, or a .ttf/.ttc file path")
    common.add_argument("--fps", type=int, default=10, help="frame rate for gif output")
    common.add_argument("--out", required=True, help="output .png or .gif")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    pair = sub.add_parser("pair", parents=[common], help="before and after side by side")
    pair.add_argument("before")
    pair.add_argument("after")
    pair.add_argument("--caption", required=True)
    pair.add_argument("--size", type=int, default=22, help="caption font size in px")

    gif = sub.add_parser("gif", parents=[common], help="caption bar on a video or gif")
    gif.add_argument("input")
    gif.add_argument("--caption", required=True)
    gif.add_argument("--width", type=int, default=480, help="output width in px")
    gif.add_argument("--size", type=int, default=18, help="caption font size in px")

    sheet = sub.add_parser("sheet", parents=[common], help="labelled grid of views")
    sheet.add_argument("views", nargs="+", help="PATH or PATH=LABEL, one per tile")
    sheet.add_argument("--title", required=True)
    sheet.add_argument("--cols", type=int, default=3)
    sheet.add_argument("--tile-width", type=int, default=480, help="tile width in px")

    stl = sub.add_parser("stl", parents=[common],
                         help="STL rendered to six sides and iso, with labels")
    stl.add_argument("stl", help="the part, as a binary STL")
    stl.add_argument("--before", help="the part before the change. Adds a BEFORE row.")
    stl.add_argument("--title", required=True)
    stl.add_argument("--tile-width", type=int, default=240, help="tile width in px")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    need("ffmpeg")
    need("ffprobe")
    commands = {"pair": cmd_pair, "gif": cmd_gif, "sheet": cmd_sheet, "stl": cmd_stl}
    with tempfile.TemporaryDirectory() as folder:
        commands[args.cmd](args, folder)


if __name__ == "__main__":
    main()
