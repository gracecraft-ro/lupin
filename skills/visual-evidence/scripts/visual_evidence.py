#!/usr/bin/env python3
"""Add captions and layouts to visual evidence. Needs ffmpeg and ffprobe.

Run it in the dev shell. It puts ffmpeg, fontconfig (fc-match), node, rsvg-convert, and the DejaVu faces on PATH:

  nix develop -c python3 scripts/visual_evidence.py pair \
    before.png after.png --title "Save button" \
    --caption "The save button moves to the bottom bar" --out pair.png

  nix develop -c python3 scripts/visual_evidence.py gif \
    demo.mp4 --caption "Tap Save to confirm" --out demo.gif

  nix develop -c python3 scripts/visual_evidence.py sheet \
    top.png="Top" front.png="Front" --title "Part A, same views" --out sheet.png

  nix develop -c python3 scripts/visual_evidence.py stl \
    part-after.stl --before part-before.stl --title "Part, before and after" --out part.png

  nix develop -c python3 scripts/visual_evidence.py drawing \
    cut-after.dxf --before cut-before.dxf --title "Cut sheet, before and after" --out drawing.png

  nix develop -c python3 scripts/visual_evidence.py pdf \
    after.pdf --before before.pdf --title "Print, before and after" --out pdf.png

Each command prints one line: wrote PATH WIDTHxHEIGHT bytes=N
Open the file and check it. The script does not judge the picture.
"""

import argparse
import glob
import json
import os
import re
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
LEGEND_GAP = 26  # px between a sheet and its legend
LEGEND_ENTRY_GAP = 16  # px between legend entries
TITLE_GAP = 14  # px between a title and the hairline under it, and from the hairline to the next block
CAPTION_GAP = 26  # px between a caption and the image labels
LABEL_PAD = 8  # px between a label and the top or bottom of its label strip
MIN_LABEL = 9  # px. A label shrinks no further than this to fit its image
TITLE_PX = 44  # title size in px. Fixed, so captions and labels match across commands
CAPTION_PX = 34  # caption size in px, unless --size sets it
LABEL_PX = 24  # label size in px, before fit_label_size shrinks a long one
# Ground and ink colours per theme, from themes.json. The renderer reads the same file.
THEMES = json.load(
    open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "themes.json"),
        encoding="utf-8",
    )
)
CANVAS = FRAME = RULE = TITLE_INK = CAPTION_INK = LABEL_INK = ""


def apply_theme(name):
    """Set the ground and ink colours from THEMES[name]. Values become ffmpeg colours."""
    global CANVAS, FRAME, RULE, TITLE_INK, CAPTION_INK, LABEL_INK
    colour = {
        key: "0x" + value[1:]
        for key, value in THEMES[name].items()
        if value.startswith("#")
    }
    CANVAS, FRAME, RULE = colour["canvas"], colour["frame"], colour["rule"]
    TITLE_INK, CAPTION_INK, LABEL_INK = (
        colour["title"],
        colour["caption"],
        colour["label"],
    )


apply_theme("paper")
RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_stl.mjs")
STL_SIZE = 800  # px per rendered view, before tiling
STL_VIEWS = [
    ("iso", "ISO"),
    ("iso-cutaway", "ISO CUT"),
    ("plus-x", "+X"),
    ("minus-x", "-X"),
    ("plus-y", "+Y"),
    ("minus-y", "-Y"),
    ("plus-z", "+Z"),
    ("minus-z", "-Z"),
]


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
        die(
            "fc-match not found. Run in the dev shell (nix develop -c), or pass --font PATH.ttf"
        )
    family = spec.split(":")[0]
    result = subprocess.run(
        ["fc-match", "-f", "%{family}\t%{file}", spec], capture_output=True, text=True
    )
    if result.returncode != 0:
        die(f"fc-match failed for font {spec!r}:\n" + result.stderr.strip())
    chosen, _, path = result.stdout.partition("\t")
    if family.lower() not in (name.strip().lower() for name in chosen.split(",")):
        die(
            f"font {family!r} is not installed. fontconfig chose {chosen!r}. Pass --font PATH.ttf"
        )
    return path.strip()


def font_option(path):
    """drawtext ignores style=, so bold needs its own .ttf file."""
    return f"fontfile={esc(path)}"


def probe(path):
    """Return (width, height) of the first video stream."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
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
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:s={width}x{height},format=gray",
            "-vf",
            caption_draw(font, name, size, PAD, PAD),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        cwd=folder,
        capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) != width * height:
        die(
            "ffmpeg failed while measuring text:\n"
            + result.stderr.decode(errors="replace").strip()
        )
    rows = [result.stdout[i : i + width] for i in range(0, width * height, width)]
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
                line = line[len(cut) :]
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
    return (
        f"drawtext={font_option(font)}:textfile={name}:x={x}:y={y}:"
        f"fontsize={size}:fontcolor={colour}:line_spacing={LINE_GAP}"
    )


def output_args(chain, out, fps):
    """Return the ffmpeg arguments that write the filtergraph to out."""
    if out.lower().endswith(".gif"):
        # -r, not the fps filter: the fps filter drops a lone still frame.
        graph = (
            f"{chain},split[s0][s1];"
            "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer"
        )
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
    while (
        size > MIN_LABEL
        and max(ink_width(text, font, size, folder) for text in unique) > max_w
    ):
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
        filters.append(
            caption_draw(faces["title"], name, title_size, MARGIN, top, TITLE_INK)
        )
        filters.append(
            f"drawbox=x={MARGIN}:y={rule_y}:w=iw-{2 * MARGIN}:h=1:color={RULE}:t=fill"
        )
        top = rule_y + 1 + TITLE_GAP
    if caption:
        lines = wrap(caption, wrap_w, faces["caption"], caption_size, folder)
        name = write_text(folder, "caption.txt", "\n".join(lines))
        filters.append(
            caption_draw(faces["caption"], name, caption_size, MARGIN, top, CAPTION_INK)
        )
        top += text_height(len(lines), caption_size) + CAPTION_GAP
    return top, filters


def parse_crop(value):
    """Parse W:H:X:Y in px, the order of ffmpeg's crop filter. None means no crop."""
    if value is None:
        return None
    parts = value.split(":")
    if (
        len(parts) != 4
        or not all(part.isdigit() for part in parts)
        or int(parts[0]) == 0
        or int(parts[1]) == 0
    ):
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
    return (
        f"pad=iw+2:ih+2:1:1:{FRAME},pad=iw:{height + 2}:0:0:{CANVAS},"
        f"pad=iw:ih+{band}:0:{band}:{CANVAS},"
        f"drawtext={font_option(font)}:text={label}:x=0:y={LABEL_PAD}:y_align=font:"
        f"fontsize={size}:fontcolor={LABEL_INK}"
    )


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
    size = fit_label_size(
        ["Before", "After"], panel_w, nominal, args.faces["label"], folder
    )
    top, head = header(
        args.faces, canvas_w, args.title, args.caption, title_size, caption_size, folder
    )
    crop_part = crop_filter(crop)
    inputs = ["-i", before, "-i", after]
    after_part = f"[1:v]{crop_part}"
    extra = []  # the legend, under the pair
    marks = parse_marks(args.mark)
    if marks:
        # Marks use the pixels of the saved image. The crop does not move them.
        x0, y0 = (crop[2], crop[3]) if crop else (0, 0)
        inputs += ["-i", mark_png(marks, (x0, y0, w2, h2), THEMES[args.theme], folder)]
        after_part = f"[1:v]{crop_part}null[pic];[pic][2:v]overlay=0:0,"
        legend = [f"{number}. {text}" for number, (_, _, text) in enumerate(marks, start=1)]
        panel_h = height + 2 + label_band(nominal)  # frame and label strip included
        extra = legend_filters(legend, row_w, panel_h, args.faces, folder, caption_size)
    chain = (
        f"[0:v]{crop_part}{panel_chain(height, 'Before', size, nominal, args.faces['label'])}[a];"
        f"{after_part}{panel_chain(height, 'After', size, nominal, args.faces['label'])}[b];"
        f"[a]pad=iw+{PAIR_GAP}:ih:0:0:{CANVAS}[a2];"
        f"[a2][b]hstack=inputs=2"
    )
    run_ffmpeg(
        inputs,
        output_args(",".join([chain, *extra, canvas_pad(top), *head]), out, args.fps),
        folder,
    )
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
        + caption_draw(
            args.faces["caption"], name, caption_size, MARGIN, f"h-{band - MARGIN}"
        )
    )
    run_ffmpeg(["-i", src], output_args(chain, out, args.fps), folder)
    report(out)


def cmd_sheet(args, folder):
    if len(args.views) < 2:
        die("sheet needs at least two views")
    items = []
    for raw in args.views:
        path, _, label = raw.partition("=")
        items.append(
            (checked_input(path), label or os.path.splitext(os.path.basename(path))[0])
        )
    make_sheet(items, args, checked_out(args.out, ".png"), folder)


def legend_filters(lines, width, height, faces, folder, caption_size=CAPTION_PX):
    """Return the filters that draw a legend under a grid of width x height px.

    Each line wraps to width px in the caption face. The next line starts below it.
    """
    wrapped = [wrap(line, width, faces["caption"], caption_size, folder) for line in lines]
    heights = [text_height(len(entry), caption_size) for entry in wrapped]
    total = sum(heights) + LEGEND_ENTRY_GAP * (len(lines) - 1)
    filters = [f"pad=iw:ih+{LEGEND_GAP + total}:0:0:{CANVAS}"]
    y = height + LEGEND_GAP
    for index, (entry, entry_h) in enumerate(zip(wrapped, heights)):
        name = write_text(folder, f"legend{index}.txt", "\n".join(entry))
        filters.append(caption_draw(faces["caption"], name, caption_size, 0, y))
        y += entry_h + LEGEND_ENTRY_GAP
    return filters


def make_sheet(items, args, out, folder, row_heads=None, legend=None):
    first_w, first_h = probe(items[0][0])
    tile_w = (
        args.tile_width or first_w
    )  # the first image's width unless --tile-width is given
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
    head_band = (
        text_height(1, title_size) + 2 * TITLE_GAP + 1 if heads else 0
    )  # heading, gap, hairline, gap
    cell_h = head_band + band + frame_h
    top, head = header(args.faces, canvas_w, args.title, None, title_size, None, folder)
    for row, text in enumerate(
        heads
    ):  # each heading is in the title face, with a hairline under it
        y = top + row * (cell_h + SHEET_GAP)
        name = write_text(folder, f"head{row}.txt", text)
        head.append(
            caption_draw(args.faces["title"], name, title_size, MARGIN, y, TITLE_INK)
        )
        head.append(
            f"drawbox=x={MARGIN}:y={y + text_height(1, title_size) + TITLE_GAP}:w=iw-{2 * MARGIN}:h=1:color={RULE}:t=fill"
        )
    chains = []
    for index, label in enumerate(labels):
        name = write_text(folder, f"tile{index}.txt", label)
        # y_align=font puts every label on one baseline. The default aligns each string's own ink top,
        # so a label with no ascenders, such as "sea", sat high.
        texts = [
            f"drawtext={font_option(args.faces['label'])}:textfile={name}:x=0:y={head_band + LABEL_PAD}:y_align=font:"
            f"fontsize={size}:fontcolor={LABEL_INK}"
        ]
        chains.append(
            f"[{index}:v]scale={tile_w}:{tile_h}:force_original_aspect_ratio=decrease,"
            f"pad={tile_w}:{tile_h}:(ow-iw)/2:(oh-ih)/2:{CANVAS},"
            f"pad={frame_w}:{frame_h}:1:1:{FRAME},"
            f"pad=iw:ih+{band + head_band}:0:{band + head_band}:{CANVAS},"
            + ",".join(texts)
            + f"[t{index}]"
        )
    layout = "|".join(
        f"{(index % cols) * (frame_w + SHEET_GAP)}_{(index // cols) * (cell_h + SHEET_GAP)}"
        for index in range(len(items))
    )
    tiles = "".join(f"[t{index}]" for index in range(len(items)))
    # xstack needs two or more inputs. One tile passes through.
    body = (
        f"{tiles}xstack=inputs={len(items)}:layout={layout}:fill={CANVAS}"
        if len(items) > 1
        else f"{tiles}null"
    )
    body_chain = [body]
    if legend:
        rows = (len(items) + cols - 1) // cols
        grid_h = rows * cell_h + (rows - 1) * SHEET_GAP
        body_chain += legend_filters(legend, grid_w, grid_h, args.faces, folder)
    chains.append(",".join([*body_chain, canvas_pad(top), *head]))
    inputs = [arg for path, _ in items for arg in ("-i", path)]
    run_ffmpeg(inputs, output_args(";".join(chains), out, args.fps), folder)
    report(out)


def render_views(
    stl, folder, frame=None, palette=None, palette_file=None, theme="paper"
):
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
    return [
        (os.path.join(folder, f"{stem}-{name}.png"), label) for name, label in STL_VIEWS
    ]


# Roundsmith writes LINE and LWPOLYLINE. Any other entity stops the command.
DXF_ENTITIES = ("LINE", "LWPOLYLINE")
# layer: (theme colour key, dash pattern). Other layers use the caption colour, solid.
DXF_STYLE = {
    "CUT": ("title", None),
    "SEAM": ("caption", "6 4"),
    "FOLD": ("label", "2 3"),
    "LABELS": ("cut", None),
}
DRAWING_PAD = 0.05  # margin round a drawing, as a share of its size
MARK_DOT = 0.012  # radius of a mark's dot on the curve, as a share of the drawing's larger side
MARK_BADGE = 0.035  # radius of the numbered badge, same share
MARK_OFFSET = 0.09  # the badge centre sits this far right and up from the dot, same share
MARK_STROKE = 0.004  # width of the leader and the badge outline, same share
SVG_ROOT = re.compile(r"<svg\b[^>]*>")


def dxf_entities(path):
    """Return (type, [(code, value), ...]) for each entity in the ENTITIES section."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    entities, section, name_next = [], None, False
    for i in range(0, len(lines) - 1, 2):
        code, value = lines[i].strip(), lines[i + 1].strip()
        if code == "0":
            name_next = value == "SECTION"
            if value == "ENDSEC":
                section = None
            elif value == "EOF":
                break
            elif section == "ENTITIES":
                entities.append((value, []))
        elif code == "2" and name_next:
            section, name_next = value, False
        elif section == "ENTITIES" and entities:
            entities[-1][1].append((code, value))
    return entities


def dxf_paths(path):
    """Return (layer, points, closed) for each LINE or LWPOLYLINE in a DXF."""
    paths = []
    for kind, data in dxf_entities(path):
        if kind not in DXF_ENTITIES:
            die(
                f"DXF entity {kind} is not supported. Supported: {', '.join(DXF_ENTITIES)}"
            )
        layer = next((value for code, value in data if code == "8"), "0")
        if kind == "LINE":
            coords = {
                code: float(value)
                for code, value in data
                if code in ("10", "20", "11", "21")
            }
            paths.append(
                (
                    layer,
                    [(coords["10"], coords["20"]), (coords["11"], coords["21"])],
                    False,
                )
            )
        else:
            xs = [float(value) for code, value in data if code == "10"]
            ys = [float(value) for code, value in data if code == "20"]
            closed = bool(
                int(next((value for code, value in data if code == "70"), "0")) & 1
            )
            paths.append((layer, list(zip(xs, ys)), closed))
    return paths


def dxf_svg(path, theme):
    """Return an SVG of a DXF. DXF is y up, so the drawing is flipped to the SVG's y down."""
    paths = dxf_paths(path)
    if not paths:
        die("the DXF has no LINE or LWPOLYLINE entities")
    xs = [x for _, points, _ in paths for x, _ in points]
    ys = [-y for _, points, _ in paths for _, y in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    pad = span * DRAWING_PAD
    view = " ".join(
        f"{v:.4f}"
        for v in (
            min(xs) - pad,
            min(ys) - pad,
            max(xs) - min(xs) + 2 * pad,
            max(ys) - min(ys) + 2 * pad,
        )
    )
    by_layer = {}
    for layer, points, closed in paths:
        d = (
            "M"
            + " L".join(f"{x:.4f} {-y:.4f}" for x, y in points)
            + (" Z" if closed else "")
        )
        by_layer.setdefault(layer, []).append(d)
    shapes = []
    for layer, ds in by_layer.items():
        key, dash = DXF_STYLE.get(layer, ("caption", None))
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        shapes.append(
            f'<path fill="none" stroke="{theme[key]}" stroke-width="{span / 500:.4f}"'
            f'{dash_attr} d="{" ".join(ds)}"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">'
        + "".join(shapes)
        + "</svg>"
    )


def drawing_svg(path, theme):
    """Return the SVG text of a .svg or .dxf file."""
    if path.lower().endswith(".dxf"):
        return dxf_svg(path, theme)
    if path.lower().endswith(".svg"):
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    die("drawing takes an .svg or a .dxf file")


def svg_view(text):
    """Return the viewBox of an SVG as (x, y, width, height)."""
    root = SVG_ROOT.search(text)
    match = re.search(r'viewBox="([^"]*)"', root.group(0)) if root else None
    if not match:
        die("the SVG has no viewBox. Add one, so its scale is known.")
    return tuple(float(value) for value in re.split(r"[\s,]+", match.group(1).strip()))


def fit_view(text, view):
    """Return the SVG with the shared viewBox and no width or height, so every file has one scale."""
    root = SVG_ROOT.search(text)
    tag = re.sub(r'\s(?:width|height|viewBox)="[^"]*"', "", root.group(0))
    return (
        text[: root.start()]
        + tag.replace("<svg", f'<svg viewBox="{view}"', 1)
        + text[root.end() :]
    )


def parse_marks(raw):
    """Return (x, y, text) for each --mark X Y TEXT. Return [] when there are none."""
    marks = []
    for x, y, text in raw or []:
        if not text.strip():
            die("--mark needs a text for the legend")
        try:
            marks.append((float(x), float(y), text))
        except ValueError:
            die(f"--mark needs numbers for X and Y, got {x!r} and {y!r}")
    return marks


def check_marks(view, points, flip, what="drawing"):
    """Stop unless every (x, y) point lies on the drawing. flip is True for a DXF, whose y points up."""
    left, top, width, height = view
    for number, (x, given_y) in enumerate(points, start=1):
        y = -given_y if flip else given_y
        if not (left <= x <= left + width and top <= y <= top + height):
            die(
                f"mark {number} at ({x:g}, {given_y:g}) is outside the {what}. Check its coordinates."
            )


def check_badges(view, points):
    """Stop unless every badge lies inside the picture. A picture cannot grow past its edge."""
    left, top, width, height = view
    reach = max(width, height) * (MARK_OFFSET + MARK_BADGE)  # the badge's far edge, from its point
    for number, (x, y) in enumerate(points, start=1):
        if x + reach > left + width or y - reach < top:
            die(
                f"mark {number} at ({x:g}, {y:g}): its badge would pass the edge of the picture. Move the point inward."
            )


def mark_view(view, points, flip):
    """Return the view grown so every mark's badge fits. The badge sits up and right of its dot.

    The room is a share of the span, and growing the view changes the span. So repeat until it settles.
    """
    left, top, width, height = view
    room = max(width, height) * (MARK_OFFSET + MARK_BADGE)
    for _ in range(10):
        l, t, r, b = left, top, left + width, top + height
        for x, given_y in points:
            y = -given_y if flip else given_y
            l, r = min(l, x - room), max(r, x + room)
            t, b = min(t, y - room), max(b, y + room)
        room = max(r - l, b - t) * (MARK_OFFSET + MARK_BADGE)
    return l, t, r - l, b - t


def mark_svg(text, points, view, theme, flip):
    """Return the SVG with a numbered mark at each (x, y) point, in the SVG's own units.

    A mark is a dot on the curve, a leader line, and a badge with its number. view is
    (x, y, width, height) of the viewBox. Check the points first. flip is True for a DXF.
    """
    span = max(view[2], view[3])
    ink, ground = theme["title"], theme["canvas"]
    stroke = f"{span * MARK_STROKE:.4f}"
    shapes = []
    for number, (x, given_y) in enumerate(points, start=1):
        y = -given_y if flip else given_y
        bx, by = x + span * MARK_OFFSET, y - span * MARK_OFFSET
        shapes.append(
            f'<line x1="{x:.4f}" y1="{y:.4f}" x2="{bx:.4f}" y2="{by:.4f}" stroke="{ink}" stroke-width="{stroke}"/>'
            f'<circle cx="{x:.4f}" cy="{y:.4f}" r="{span * MARK_DOT:.4f}" fill="{ink}"/>'
            f'<circle cx="{bx:.4f}" cy="{by:.4f}" r="{span * MARK_BADGE:.4f}" fill="{ground}" stroke="{ink}" stroke-width="{stroke}"/>'
            f'<text x="{bx:.4f}" y="{by:.4f}" font-family="DejaVu Sans Mono" font-weight="bold" '
            f'font-size="{span * MARK_BADGE * 1.3:.4f}" text-anchor="middle" dominant-baseline="central" fill="{ink}">{number}</text>'
        )
    end = text.rfind("</svg>")
    if end < 0:
        die("the SVG has no closing </svg> tag. Marks cannot be added.")
    return text[:end] + '<g id="marks">' + "".join(shapes) + "</g>" + text[end:]


def mark_png(marks, region, theme, folder):
    """Return a PNG of the marks, the size of region, with transparent pixels elsewhere.

    region is (x, y, width, height) in the picture's pixels. Each mark is an (x, y, text) in those pixels.
    """
    need("rsvg-convert")
    points = [(x, y) for x, y, _ in marks]
    check_marks(region, points, False, what="picture")
    check_badges(region, points)
    left, top, width, height = region
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="{left} {top} {width} {height}"></svg>'
    )
    svg_path = os.path.join(folder, "marks.svg")
    png_path = os.path.join(folder, "marks.png")
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(mark_svg(svg, points, region, theme, flip=False))
    result = subprocess.run(
        ["rsvg-convert", "--output", png_path, svg_path], capture_output=True, text=True
    )
    if result.returncode != 0:
        die("rsvg-convert failed:\n" + result.stderr.strip())
    return png_path


def cmd_drawing(args, folder):
    need("rsvg-convert")
    out = checked_out(args.out, ".png")
    theme = THEMES[args.theme]
    if args.before:
        inputs = [
            (checked_input(args.before), "Before"),
            (checked_input(args.file), "After"),
        ]
    else:
        inputs = [(checked_input(args.file), os.path.basename(args.file))]
    texts = [drawing_svg(path, theme) for path, _ in inputs]
    views = [svg_view(text) for text in texts]
    left = min(v[0] for v in views)
    top = min(v[1] for v in views)
    right = max(v[0] + v[2] for v in views)
    bottom = max(v[1] + v[3] for v in views)
    shared = (left, top, right - left, bottom - top)
    marks = parse_marks(args.mark)
    points = [(x, y) for x, y, _ in marks]
    flip = args.file.lower().endswith(".dxf")
    check_marks(shared, points, flip)
    if marks:  # badges may pass the drawing edge, so the view grows to hold them
        shared = mark_view(shared, points, flip)
    view = " ".join(f"{v:.4f}" for v in shared)  # one viewBox, one scale
    items = []
    for index, (text, (_, label)) in enumerate(zip(texts, inputs)):
        svg = os.path.join(folder, f"drawing{index}.svg")
        png = os.path.join(folder, f"drawing{index}.png")
        fitted = fit_view(text, view)
        if marks and index == len(inputs) - 1:  # only the after drawing carries marks
            fitted = mark_svg(fitted, points, shared, theme, flip)
        with open(svg, "w", encoding="utf-8") as handle:
            handle.write(fitted)
        result = subprocess.run(
            [
                "rsvg-convert",
                "--width",
                str(args.tile_width),
                "--background-color",
                theme["canvas"],
                "--output",
                png,
                svg,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            die("rsvg-convert failed:\n" + result.stderr.strip())
        items.append((png, label))
    sheet = argparse.Namespace(
        title=args.title,
        cols=len(items),
        tile_width=args.tile_width,
        faces=args.faces,
        fps=args.fps,
    )
    legend = [f"{number}. {text}" for number, (_, _, text) in enumerate(marks, start=1)]
    make_sheet(items, sheet, out, folder, legend=legend or None)


def cmd_stl(args, folder):
    need("node")
    new = checked_input(args.stl)
    out = checked_out(args.out, ".png")
    if args.palette and args.palette_file:
        die("use --palette or --palette-file, not both")
    palette_file = checked_input(args.palette_file) if args.palette_file else None
    if args.before:
        before = checked_input(args.before)
        before_views = render_views(
            before,
            os.path.join(folder, "before"),
            frame=new,
            palette=args.palette,
            palette_file=palette_file,
            theme=args.theme,
        )
        after_views = render_views(
            new,
            os.path.join(folder, "after"),
            frame=before,
            palette=args.palette,
            palette_file=palette_file,
            theme=args.theme,
        )
        items = before_views + after_views
    else:
        items = render_views(
            new,
            os.path.join(folder, "new"),
            palette=args.palette,
            palette_file=palette_file,
            theme=args.theme,
        )
    # One row for one part. Two rows, before then after, when --before is given.
    sheet = argparse.Namespace(
        title=args.title,
        cols=8,
        tile_width=args.tile_width,
        faces=args.faces,
        fps=args.fps,
    )
    make_sheet(
        items,
        sheet,
        out,
        folder,
        row_heads=["Before", "After"] if args.before else None,
    )


def pdf_pages(path, folder, name):
    """Render each page of a PDF at 150 dpi. Return the PNG paths in page order."""
    prefix = os.path.join(folder, name)
    result = subprocess.run(
        ["pdftoppm", "-r", "150", "-png", path, prefix], capture_output=True, text=True
    )
    if result.returncode != 0:
        die("pdftoppm failed:\n" + result.stderr.strip())
    pages = glob.glob(glob.escape(prefix) + "-*.png")
    return sorted(
        pages, key=lambda page: int(re.search(r"-(\d+)\.png$", page).group(1))
    )


def cmd_pdf(args, folder):
    need("pdftoppm")
    out = checked_out(args.out, ".png")
    if args.before:
        inputs = [
            ("Before", checked_input(args.before)),
            ("After", checked_input(args.file)),
        ]
    else:
        inputs = [(None, checked_input(args.file))]
    rows = [
        (heading, pdf_pages(path, folder, f"row{index}"))
        for index, (heading, path) in enumerate(inputs)
    ]
    counts = [len(pages) for _, pages in rows]
    if 0 in counts:
        die("a PDF has no pages")
    if len(set(counts)) > 1:
        die(
            f"the PDFs have different page counts {counts}. Use the same number of pages."
        )
    # Roundsmith keeps each page at its own size. Pad every page to the largest, top-left, so each keeps its
    # true scale when the sheet scales the tiles.
    sizes = [probe(page) for _, pages in rows for page in pages]
    width, height = max(w for w, _ in sizes), max(h for _, h in sizes)
    items = []
    for index, (_, pages) in enumerate(rows):
        for number, page in enumerate(pages, start=1):
            padded = os.path.join(folder, f"pad{index}-{number}.png")
            run_ffmpeg(
                ["-i", page],
                ["-vf", f"pad={width}:{height}:0:0:{CANVAS}", "-frames:v", "1", padded],
                folder,
            )
            items.append((padded, f"Page {number}"))
    sheet = argparse.Namespace(
        title=args.title,
        cols=counts[0],
        tile_width=args.tile_width,
        faces=args.faces,
        fps=args.fps,
    )
    make_sheet(
        items,
        sheet,
        out,
        folder,
        row_heads=[heading for heading, _ in rows] if args.before else None,
    )


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--font",
        help="one face for all text: a fontconfig name or a .ttf path "
        "(default: serif titles and captions, bold sans labels)",
    )
    common.add_argument("--fps", type=int, default=10, help="frame rate for gif output")
    common.add_argument("--out", required=True, help="output .png or .gif")
    common.add_argument(
        "--theme",
        default="paper",
        choices=sorted(THEMES),
        help="ground, ink, cut face, and default palette. Names are in themes.json",
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    pair = sub.add_parser(
        "pair", parents=[common], help="before and after side by side"
    )
    pair.add_argument("before")
    pair.add_argument("after")
    pair.add_argument("--title", help="names the whole picture, above the caption")
    pair.add_argument("--caption", required=True, help="says what changed")
    pair.add_argument(
        "--crop", help="W:H:X:Y in px, applied to both panels, for a close-up"
    )
    pair.add_argument(
        "--size", type=int, help=f"caption font size in px (default: {CAPTION_PX})"
    )
    pair.add_argument(
        "--mark",
        nargs=3,
        action="append",
        metavar=("X", "Y", "TEXT"),
        help="number a point on the after picture and add TEXT to the legend. "
        "X and Y are pixels from the top-left corner of the saved image. Repeat for each mark.",
    )

    gif = sub.add_parser("gif", parents=[common], help="caption bar on a video or gif")
    gif.add_argument("input")
    gif.add_argument("--caption", required=True)
    gif.add_argument("--width", type=int, default=480, help="output width in px")
    gif.add_argument(
        "--size", type=int, help=f"caption font size in px (default: {CAPTION_PX})"
    )

    sheet = sub.add_parser("sheet", parents=[common], help="labelled grid of views")
    sheet.add_argument("views", nargs="+", help="PATH or PATH=LABEL, one per tile")
    sheet.add_argument("--title", required=True)
    sheet.add_argument("--cols", type=int, default=3)
    sheet.add_argument(
        "--tile-width",
        type=int,
        help="tile width in px (default: the first image's width)",
    )

    stl = sub.add_parser(
        "stl",
        parents=[common],
        help="STL rendered to six sides, iso, and iso cutaway, with labels",
    )
    stl.add_argument("stl", help="the part, as a binary STL")
    stl.add_argument("--before", help="the part before the change. Adds a Before row.")
    stl.add_argument("--title", required=True)
    stl.add_argument("--tile-width", type=int, default=240, help="tile width in px")
    stl.add_argument(
        "--palette",
        help="colour set: rainbow, sunset, sea, meadow, berry, emerald, orchard, pine, or straw. "
        "Default: the theme's own",
    )
    stl.add_argument(
        "--palette-file",
        help="your own colours: JSON with 3 hex anchors and an optional base colour",
    )

    drawing = sub.add_parser(
        "drawing",
        parents=[common],
        help="SVG or DXF drawing, before and after, at one scale",
    )
    drawing.add_argument("file", help="the drawing after the change, as .svg or .dxf")
    drawing.add_argument(
        "--before",
        help="the drawing before the change, as .svg or .dxf. Adds a Before tile.",
    )
    drawing.add_argument("--title", required=True)
    drawing.add_argument("--tile-width", type=int, default=800, help="tile width in px")
    drawing.add_argument(
        "--mark",
        nargs=3,
        action="append",
        metavar=("X", "Y", "TEXT"),
        help="number a point on the after drawing and add TEXT to the legend. "
        "X and Y are in the drawing's units. Repeat for each mark.",
    )

    pdf = sub.add_parser(
        "pdf", parents=[common], help="PDF pages, before and after, at one scale"
    )
    pdf.add_argument("file", help="the PDF after the change")
    pdf.add_argument("--before", help="the PDF before the change. Adds a Before row.")
    pdf.add_argument("--title", required=True)
    pdf.add_argument("--tile-width", type=int, default=600, help="tile width in px")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    apply_theme(args.theme)
    need("ffmpeg")
    need("ffprobe")
    specs = {role: args.font for role in FONTS} if args.font else FONTS
    args.faces = {role: font_file(spec) for role, spec in specs.items()}
    commands = {
        "pair": cmd_pair,
        "gif": cmd_gif,
        "sheet": cmd_sheet,
        "stl": cmd_stl,
        "drawing": cmd_drawing,
        "pdf": cmd_pdf,
    }
    with tempfile.TemporaryDirectory() as folder:
        commands[args.cmd](args, folder)


if __name__ == "__main__":
    main()
