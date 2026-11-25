#!/usr/bin/env python3
"""Add captions and layouts to visual evidence. Needs ffmpeg and ffprobe.

Run it in the dev shell. It puts ffmpeg, fontconfig (fc-match), node, and the DejaVu faces on PATH:

  nix develop -c python3 scripts/visual_evidence.py pair \
    before.png after.png --title "Save button" \
    --caption "The save button moves to the bottom bar" --out pair.png

  nix develop -c python3 scripts/visual_evidence.py gif \
    demo.mp4 --caption "Tap Save to confirm" --out demo.gif

  nix develop -c python3 scripts/visual_evidence.py sheet \
    top.png="Top" front.png="Front" --title "Part A, same views" --out sheet.png

  nix develop -c python3 scripts/visual_evidence.py stl \
    part-after.stl --before part-before.stl --title "Part, before and after" --out part.png

Each command prints one line: wrote PATH WIDTHxHEIGHT bytes=N
Open the file and check it. The script does not judge the picture.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# fontconfig name and style for each text role. --font replaces all three.
FONTS = {
    "title": "DejaVu Serif:bold",
    "caption": "DejaVu Serif",
    "label": "DejaVu Sans Mono:bold",
}
PAD = 8  # px of padding used when measuring text
LINE_GAP = 6  # px between text lines
MAX_LINES = 4  # a caption or title longer than this is too long to read at this size
MARGIN = 24  # px of canvas around the content
PAIR_GAP = 32  # px between the two pair panels
SHEET_GAP = 24  # px between sheet tiles, across and down
TITLE_GAP = 14  # px between a title and the hairline under it, and from the hairline to the next block
CAPTION_GAP = 26  # px between a caption and the image labels
LABEL_PAD = 8  # px between a label and the top or bottom of its label strip
MIN_LABEL = 9  # px. A label shrinks no further than this to fit its image
TITLE_PX = 44  # title size in px. Fixed, so captions and labels match across commands
CAPTION_PX = 34  # caption size in px, unless --size sets it
LABEL_PX = 24  # label size in px, before fit_label_size shrinks a long one
# Ground and ink colours per theme, from themes.json. The renderer reads the same file.
THEMES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes.json"),
                        encoding="utf-8"))
CANVAS = FRAME = RULE = TITLE_INK = CAPTION_INK = LABEL_INK = ""


def apply_theme(name):
    """Set the ground and ink colours from THEMES[name]. Values become ffmpeg colours."""
    global CANVAS, FRAME, RULE, TITLE_INK, CAPTION_INK, LABEL_INK
    colour = {key: "0x" + value[1:] for key, value in THEMES[name].items() if value.startswith("#")}
    CANVAS, FRAME, RULE = colour["canvas"], colour["frame"], colour["rule"]
    TITLE_INK, CAPTION_INK, LABEL_INK = colour["title"], colour["caption"], colour["label"]


apply_theme("paper")
RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_stl.mjs")
STL_SIZE = 800  # px per rendered view, before tiling
STL_VIEWS = [("iso", "ISO"), ("iso-cutaway", "ISO CUT"), ("plus-x", "+X"), ("minus-x", "-X"),
             ("plus-y", "+Y"), ("minus-y", "-Y"), ("plus-z", "+Z"), ("minus-z", "-Z")]


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


def font_file(spec):
    """Return the .ttf path for a family name such as "DejaVu Serif:bold", or for a path.

    fc-match swaps in another face when a name is unknown, so check the family it chose.
    """
    if os.path.isfile(spec):
        return os.path.abspath(spec)
    if shutil.which("fc-match") is None:
        die("fc-match not found. Run in the dev shell (nix develop -c), or pass --font PATH.ttf")
    family = spec.split(":")[0]
    result = subprocess.run(["fc-match", "-f", "%{family}\t%{file}", spec],
                            capture_output=True, text=True)
    if result.returncode != 0:
        die(f"fc-match failed for font {spec!r}:\n" + result.stderr.strip())
    chosen, _, path = result.stdout.partition("\t")
    if family.lower() not in (name.strip().lower() for name in chosen.split(",")):
        die(f"font {family!r} is not installed. fontconfig chose {chosen!r}. Pass --font PATH.ttf")
    return path.strip()


def font_option(path):
    """drawtext ignores style=, so bold needs its own .ttf file."""
    return f"fontfile={esc(path)}"


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


def text_height(line_count, size):
    return line_count * size + (line_count - 1) * LINE_GAP


def write_text(folder, name, text):
    """ffmpeg reads text from a file. This avoids escaping the caption."""
    with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
        handle.write(text)
    return name


def caption_draw(font, name, size, x, y, colour=CAPTION_INK):
    return (f"drawtext={font_option(font)}:textfile={name}:x={x}:y={y}:"
            f"fontsize={size}:fontcolor={colour}:line_spacing={LINE_GAP}")


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


def type_sizes(caption=None):
    """Return (title, caption, label) sizes in px. The sizes are fixed, so every command matches.

    caption is the size from --size. None means use CAPTION_PX.
    """
    return TITLE_PX, caption or CAPTION_PX, LABEL_PX


def label_band(nominal):
    """Height of the strip above an image that holds its label."""
    return nominal + 2 * LABEL_PAD


def fit_label_size(labels, max_w, size, font, folder):
    """Shrink the labels until the longest fits max_w. The strip keeps its nominal height."""
    unique = set(labels)
    while size > MIN_LABEL and max(ink_width(text, font, size, folder) for text in unique) > max_w:
        size -= 1
    return size


def canvas_pad(top):
    """Add the margin on every side. top px above the content hold the header."""
    return f"pad=iw+{2 * MARGIN}:ih+{top + MARGIN}:{MARGIN}:{top}:{CANVAS}"


def header(faces, canvas_w, title, caption, title_size, caption_size, folder):
    """Return (top, filters) for the title, its hairline, and the caption.

    top is the y where the content starts. The filters draw on the canvas after canvas_pad.
    """
    top = MARGIN
    filters = []
    wrap_w = canvas_w - 2 * MARGIN
    if title:
        lines = wrap(title, wrap_w, faces["title"], title_size, folder)
        name = write_text(folder, "title.txt", "\n".join(lines))
        height = text_height(len(lines), title_size)
        rule_y = top + height + TITLE_GAP
        filters.append(caption_draw(faces["title"], name, title_size, MARGIN, top, TITLE_INK))
        filters.append(f"drawbox=x={MARGIN}:y={rule_y}:w=iw-{2 * MARGIN}:h=1:color={RULE}:t=fill")
        top = rule_y + 1 + TITLE_GAP
    if caption:
        lines = wrap(caption, wrap_w, faces["caption"], caption_size, folder)
        name = write_text(folder, "caption.txt", "\n".join(lines))
        filters.append(caption_draw(faces["caption"], name, caption_size, MARGIN, top, CAPTION_INK))
        top += text_height(len(lines), caption_size) + CAPTION_GAP
    return top, filters


def parse_crop(value):
    """Parse W:H:X:Y in px, the order of ffmpeg's crop filter. None means no crop."""
    if value is None:
        return None
    parts = value.split(":")
    if len(parts) != 4 or not all(part.isdigit() for part in parts) or int(parts[0]) == 0 or int(parts[1]) == 0:
        die("--crop needs W:H:X:Y in px, for example 600:400:900:700")
    return tuple(int(part) for part in parts)


def cropped_size(size, crop):
    """Return (width, height) left after the crop. Stop if the crop is outside the image."""
    if crop is None:
        return size
    width, height, x, y = crop
    if x + width > size[0] or y + height > size[1]:
        die(f"--crop is outside the {size[0]}x{size[1]} image")
    return width, height


def crop_filter(crop):
    return "" if crop is None else f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]},"


def panel_chain(height, label, size, nominal, font):
    """Frame a panel, pad it to the shared height, then add its label strip above."""
    band = label_band(nominal)
    return (f"pad=iw+2:ih+2:1:1:{FRAME},pad=iw:{height + 2}:0:0:{CANVAS},"
            f"pad=iw:ih+{band}:0:{band}:{CANVAS},"
            f"drawtext={font_option(font)}:text={label}:x=0:y={LABEL_PAD}:y_align=font:"
            f"fontsize={size}:fontcolor={LABEL_INK}")


def cmd_pair(args, folder):
    before = checked_input(args.before)
    after = checked_input(args.after)
    out = checked_out(args.out, ".png")
    crop = parse_crop(args.crop)
    (w1, h1), (w2, h2) = (cropped_size(probe(path), crop) for path in (before, after))
    height = max(h1, h2)  # nothing is scaled; the shorter panel gets canvas below it
    row_w = (w1 + 2) + PAIR_GAP + (w2 + 2)  # +2 for each 1 px frame
    canvas_w = row_w + 2 * MARGIN
    title_size, caption_size, nominal = type_sizes(args.size)
    panel_w = min(w1, w2)
    size = fit_label_size(["Before", "After"], panel_w, nominal, args.faces["label"], folder)
    top, head = header(args.faces, canvas_w, args.title, args.caption, title_size, caption_size, folder)
    crop_part = crop_filter(crop)
    chain = (
        f"[0:v]{crop_part}{panel_chain(height, 'Before', size, nominal, args.faces['label'])}[a];"
        f"[1:v]{crop_part}{panel_chain(height, 'After', size, nominal, args.faces['label'])}[b];"
        f"[a]pad=iw+{PAIR_GAP}:ih:0:0:{CANVAS}[a2];"
        f"[a2][b]hstack=inputs=2"
    )
    run_ffmpeg(["-i", before, "-i", after],
               output_args(",".join([chain, canvas_pad(top), *head]), out, args.fps), folder)
    report(out)


def cmd_gif(args, folder):
    src = checked_input(args.input)
    out = checked_out(args.out, ".gif")
    _, caption_size, _ = type_sizes(args.size)
    lines = wrap(args.caption, args.width, args.faces["caption"], caption_size, folder)
    band = text_height(len(lines), caption_size) + 2 * MARGIN
    name = write_text(folder, "caption.txt", "\n".join(lines))
    chain = (
        f"[0:v]scale={args.width}:-2:flags=lanczos,"
        f"pad=iw+{2 * MARGIN}:ih+{MARGIN}:{MARGIN}:{MARGIN}:{CANVAS},"
        f"pad=iw:ih+{band}:0:0:{CANVAS},"
        + caption_draw(args.faces["caption"], name, caption_size, MARGIN, f"h-{band - MARGIN}")
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


def make_sheet(items, args, out, folder, row_heads=None):
    first_w, first_h = probe(items[0][0])
    tile_w = args.tile_width or first_w  # the first image's width unless --tile-width is given
    tile_h = round(tile_w * first_h / first_w) // 2 * 2
    frame_w, frame_h = tile_w + 2, tile_h + 2  # +2 for the 1 px frame
    cols = min(args.cols, len(items))
    grid_w = cols * frame_w + (cols - 1) * SHEET_GAP
    canvas_w = grid_w + 2 * MARGIN
    title_size, _, nominal = type_sizes()
    labels = [label for _, label in items]
    heads = row_heads or []
    size = fit_label_size(labels, frame_w, nominal, args.faces["label"], folder)
    band = label_band(nominal)
    head_band = text_height(1, title_size) + 2 * TITLE_GAP + 1 if heads else 0  # heading, gap, hairline, gap
    cell_h = head_band + band + frame_h
    top, head = header(args.faces, canvas_w, args.title, None, title_size, None, folder)
    for row, text in enumerate(heads):  # each heading is in the title face, with a hairline under it
        y = top + row * (cell_h + SHEET_GAP)
        name = write_text(folder, f"head{row}.txt", text)
        head.append(caption_draw(args.faces["title"], name, title_size, MARGIN, y, TITLE_INK))
        head.append(f"drawbox=x={MARGIN}:y={y + text_height(1, title_size) + TITLE_GAP}:w=iw-{2 * MARGIN}:h=1:color={RULE}:t=fill")
    chains = []
    for index, label in enumerate(labels):
        name = write_text(folder, f"tile{index}.txt", label)
        # y_align=font puts every label on one baseline. The default aligns each string's own ink top,
        # so a label with no ascenders, such as "sea", sat high.
        texts = [f"drawtext={font_option(args.faces['label'])}:textfile={name}:x=0:y={head_band + LABEL_PAD}:y_align=font:"
                 f"fontsize={size}:fontcolor={LABEL_INK}"]
        chains.append(
            f"[{index}:v]scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease,"
            f"pad={tile_w}:{tile_h}:(ow-iw)/2:(oh-ih)/2:{CANVAS},"
            f"pad={frame_w}:{frame_h}:1:1:{FRAME},"
            f"pad=iw:ih+{band + head_band}:0:{band + head_band}:{CANVAS},"
            + ",".join(texts) + f"[t{index}]"
        )
    layout = "|".join(
        f"{(index % cols) * (frame_w + SHEET_GAP)}_{(index // cols) * (cell_h + SHEET_GAP)}"
        for index in range(len(items))
    )
    tiles = "".join(f"[t{index}]" for index in range(len(items)))
    body = f"{tiles}xstack=inputs={len(items)}:layout={layout}:fill={CANVAS}"
    chains.append(",".join([body, canvas_pad(top), *head]))
    inputs = [arg for path, _ in items for arg in ("-i", path)]
    run_ffmpeg(inputs, output_args(";".join(chains), out, args.fps), folder)
    report(out)


def render_views(stl, folder, frame=None, palette=None, palette_file=None, theme="paper"):
    """Render the eight views of stl. Return (path, label) pairs in STL_VIEWS order.

    frame is another STL. Both files then share one scale and centre, so a size change shows.
    palette is a palette name, or palette_file is a JSON palette. None for both uses the default.
    """
    os.makedirs(folder, exist_ok=True)
    cmd = ["node", RENDER, stl, folder, "--size", str(STL_SIZE)]
    if frame:
        cmd += ["--frame", frame]
    if palette_file:
        cmd += ["--palette-file", palette_file]
    elif palette is not None:
        cmd += ["--palette", palette]
    cmd += ["--theme", theme]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die("render_stl failed:\n" + result.stderr.strip())
    stem = os.path.splitext(os.path.basename(stl))[0]
    return [(os.path.join(folder, f"{stem}-{name}.png"), label) for name, label in STL_VIEWS]


def cmd_stl(args, folder):
    need("node")
    new = checked_input(args.stl)
    out = checked_out(args.out, ".png")
    if args.palette and args.palette_file:
        die("use --palette or --palette-file, not both")
    palette_file = checked_input(args.palette_file) if args.palette_file else None
    if args.before:
        before = checked_input(args.before)
        before_views = render_views(before, os.path.join(folder, "before"), frame=new,
                                    palette=args.palette, palette_file=palette_file, theme=args.theme)
        after_views = render_views(new, os.path.join(folder, "after"), frame=before,
                                   palette=args.palette, palette_file=palette_file, theme=args.theme)
        items = before_views + after_views
    else:
        items = render_views(new, os.path.join(folder, "new"), palette=args.palette,
                             palette_file=palette_file, theme=args.theme)
    # One row for one part. Two rows, before then after, when --before is given.
    sheet = argparse.Namespace(title=args.title, cols=8, tile_width=args.tile_width,
                               faces=args.faces, fps=args.fps)
    make_sheet(items, sheet, out, folder, row_heads=["Before", "After"] if args.before else None)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--font", help="one face for all text: a fontconfig name or a .ttf path "
                                       "(default: serif titles and captions, bold sans labels)")
    common.add_argument("--fps", type=int, default=10, help="frame rate for gif output")
    common.add_argument("--out", required=True, help="output .png or .gif")
    common.add_argument("--theme", default="paper", choices=sorted(THEMES),
                        help="ground, ink, cut face, and default palette. Names are in themes.json")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    pair = sub.add_parser("pair", parents=[common], help="before and after side by side")
    pair.add_argument("before")
    pair.add_argument("after")
    pair.add_argument("--title", help="names the whole picture, above the caption")
    pair.add_argument("--caption", required=True, help="says what changed")
    pair.add_argument("--crop", help="W:H:X:Y in px, applied to both panels, for a close-up")
    pair.add_argument("--size", type=int, help=f"caption font size in px (default: {CAPTION_PX})")

    gif = sub.add_parser("gif", parents=[common], help="caption bar on a video or gif")
    gif.add_argument("input")
    gif.add_argument("--caption", required=True)
    gif.add_argument("--width", type=int, default=480, help="output width in px")
    gif.add_argument("--size", type=int, help=f"caption font size in px (default: {CAPTION_PX})")

    sheet = sub.add_parser("sheet", parents=[common], help="labelled grid of views")
    sheet.add_argument("views", nargs="+", help="PATH or PATH=LABEL, one per tile")
    sheet.add_argument("--title", required=True)
    sheet.add_argument("--cols", type=int, default=3)
    sheet.add_argument("--tile-width", type=int,
                       help="tile width in px (default: the first image's width)")

    stl = sub.add_parser("stl", parents=[common],
                         help="STL rendered to six sides, iso, and iso cutaway, with labels")
    stl.add_argument("stl", help="the part, as a binary STL")
    stl.add_argument("--before", help="the part before the change. Adds a Before row.")
    stl.add_argument("--title", required=True)
    stl.add_argument("--tile-width", type=int, default=240, help="tile width in px")
    stl.add_argument("--palette",
                     help="colour set: rainbow, sunset, sea, meadow, berry, emerald, orchard, pine, or straw. "
                          "Default: the theme's own")
    stl.add_argument("--palette-file",
                     help="your own colours: JSON with 3 hex anchors and an optional base colour")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    apply_theme(args.theme)
    need("ffmpeg")
    need("ffprobe")
    specs = {role: args.font for role in FONTS} if args.font else FONTS
    args.faces = {role: font_file(spec) for role, spec in specs.items()}
    commands = {"pair": cmd_pair, "gif": cmd_gif, "sheet": cmd_sheet, "stl": cmd_stl}
    with tempfile.TemporaryDirectory() as folder:
        commands[args.cmd](args, folder)


if __name__ == "__main__":
    main()
