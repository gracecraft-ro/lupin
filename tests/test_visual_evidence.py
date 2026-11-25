"""Tests for the visual-evidence skill scripts.

The ffmpeg tests skip without ffmpeg and ffprobe. The render tests skip
without node.
"""

import importlib.util
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "visual-evidence" / "scripts"
TOOL = SCRIPTS / "visual_evidence.py"
RENDER = SCRIPTS / "render_stl.mjs"
GROUND_CENTRE = (251, 250, 247)  # the renderer's ground at its centre
GROUND_EDGE = (234, 231, 225)  # the renderer's ground at its corners
CUT_FACE = (236, 166, 112)  # the renderer's colour for the inside of a cut

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
HAVE_NODE = shutil.which("node") is not None

spec = importlib.util.spec_from_file_location("visual_evidence", TOOL)
ve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ve)


def run_tool(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


def make_png(folder, name, width, height, color):
    path = os.path.join(folder, name)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}",
         "-frames:v", "1", path],
        check=True,
    )
    return path


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg and ffprobe")
class FontTests(unittest.TestCase):
    def test_bold_title_resolves_to_the_bold_file(self):
        self.assertTrue(ve.font_file(ve.FONTS["title"]).endswith("Bold.ttf"))

    def test_unknown_family_stops_instead_of_substituting(self):
        with self.assertRaises(SystemExit):
            ve.font_file("No Such Font Xyz")


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg and ffprobe")
class WrapTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)
        self.face = ve.font_file(ve.FONTS["caption"])

    def fits(self, line, width):
        return ve.ink_width(line, self.face,22, self.folder) <= width

    def test_wrap_breaks_long_text_and_keeps_newlines(self):
        text = "Save button moves to the bottom bar, so it stays visible on phones"
        lines = ve.wrap(text, 300, self.face,22, self.folder)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(self.fits(line, 300) for line in lines))
        self.assertEqual(ve.wrap("one\ntwo", 1000, self.face,22, self.folder), ["one", "two"])

    def test_wrap_cuts_wide_letters_to_fit(self):
        lines = ve.wrap("W" * 40, 400, self.face,22, self.folder)
        self.assertEqual("".join(lines), "W" * 40)
        self.assertTrue(all(self.fits(line, 400) for line in lines))


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg and ffprobe")
class VisualEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder)

    def test_pair_joins_panels_and_adds_caption_band(self):
        before = make_png(self.folder, "before.png", 64, 40, "red")
        after = make_png(self.folder, "after.png", 32, 40, "blue")
        out = os.path.join(self.folder, "pair.png")
        result = run_tool("pair", before, after, "--caption", "OK", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Panels framed: 66 and 34 px, 32 px apart: 132. Canvas margin 24 each side: 180.
        # Caption at y 24, one 34 px line, 26 px gap: the content starts at y 84.
        # Label strip 40 px (24 px label, 8 px pad each side), then the 42 px frame: 82.
        # Bottom margin 24: 84 + 82 + 24 = 190.
        self.assertEqual(ve.probe(out), (180, 190))

    def test_pair_title_sits_above_caption(self):
        before = make_png(self.folder, "before.png", 64, 40, "red")
        after = make_png(self.folder, "after.png", 32, 40, "blue")
        out = os.path.join(self.folder, "pair.png")
        result = run_tool("pair", before, after, "--title", "T", "--caption", "OK", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Title 44 px, hairline, 14 px gaps: 24 + 44 + 14 + 1 + 14 = 97. Caption at y 97, 34 px line,
        # 26 px gap: content starts at y 157. Row 82. Bottom margin 24: 157 + 82 + 24 = 263.
        self.assertEqual(ve.probe(out), (180, 263))

    def test_pair_crop_takes_the_same_region_from_both_panels(self):
        before = make_png(self.folder, "before.png", 64, 40, "red")
        after = make_png(self.folder, "after.png", 64, 40, "blue")
        out = os.path.join(self.folder, "pair.png")
        result = run_tool("pair", before, after, "--crop", "40:30:4:5", "--caption", "OK", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Each crop is 40 x 30, framed 42 x 32. 42 + 32 + 42 = 116. Canvas 164 wide.
        # Caption line 34 px: content starts at y 84. Label strip 40 px + 32 px frame = 72.
        # Bottom 24: 84 + 72 + 24 = 180.
        self.assertEqual(ve.probe(out), (164, 180))

    def test_crop_outside_the_image_stops_with_message(self):
        before = make_png(self.folder, "before.png", 64, 40, "red")
        after = make_png(self.folder, "after.png", 64, 40, "blue")
        out = os.path.join(self.folder, "pair.png")
        result = run_tool("pair", before, after, "--crop", "100:30:0:0", "--caption", "OK", "--out", out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--crop is outside the 64x40 image", result.stderr)
        self.assertFalse(os.path.exists(out))

    def test_gif_keeps_width_and_adds_caption_band(self):
        src = make_png(self.folder, "src.png", 240, 120, "green")
        out = os.path.join(self.folder, "demo.gif")
        result = run_tool("gif", src, "--caption", "Tap Save", "--width", "120", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # 120 wide scales to 60 high. Canvas margin 24 each side: 168 wide, 24 px above the image.
        # At 34 px, "Tap Save" is wider than 120 px, so it wraps to two lines: 2 * 34 + 6 = 74.
        # Band 74 + 2 * 24 = 122. Total 24 + 60 + 122 = 206.
        self.assertEqual(ve.probe(out), (168, 206))

    def test_sheet_grid_and_title_band(self):
        views = [make_png(self.folder, f"v{i}.png", 60, 40, "gray") for i in range(4)]
        out = os.path.join(self.folder, "sheet.png")
        result = run_tool("sheet", *views, "--title", "Part", "--cols", "2",
                          "--tile-width", "60", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Tiles of 60 x 40 framed: 62 x 42. Label strip 40 px above each: cell 82.
        # Two across, 24 px apart: 148. Two down, 24 px apart: 188. Canvas margin 24: 196 wide.
        # Title 44 px, hairline, gaps: content starts at y 97. Bottom 24: 97 + 188 + 24 = 309.
        self.assertEqual(ve.probe(out), (196, 309))

    def test_sheet_defaults_to_the_first_image_width(self):
        views = [make_png(self.folder, f"v{i}.png", 60, 40, "gray") for i in range(4)]
        out = os.path.join(self.folder, "sheet.png")
        result = run_tool("sheet", *views, "--title", "Part", "--cols", "2", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # No --tile-width: the tiles keep the 60 px width of the first image, so the size matches
        # the explicit --tile-width test above.
        self.assertEqual(ve.probe(out), (196, 309))

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_stl_before_and_after_sheet_is_two_rows_of_eight(self):
        before = os.path.join(self.folder, "before.stl")
        after = os.path.join(self.folder, "after.stl")
        write_stl(before, box(0, 10, 0, 10, 0, 10))
        write_stl(after, box(0, 10, 0, 10, 0, 20))
        out = os.path.join(self.folder, "stl.png")
        result = run_tool("stl", after, "--before", before, "--title", "Box",
                          "--tile-width", "120", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Eight 122 px frames (120 px tile + border), 24 px apart: 8*122 + 7*24 = 1144.
        # Canvas margin 24 each side: 1192. Each row is a 73 px heading band (44 px heading,
        # 14 px gap, hairline, 14 px gap), a 40 px label strip and a 122 px frame: 235 px.
        # Two rows 24 px apart: 494. Title 44 px, hairline and gaps: content starts at y 97.
        # Bottom margin 24: 97 + 494 + 24 = 615.
        self.assertEqual(ve.probe(out), (1192, 615))

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_palette_repeats_its_colours_and_differs_from_another(self):
        part = os.path.join(self.folder, "part.stl")
        write_stl(part, box(0, 10, 0, 10, 0, 10))

        def render(palette, name):
            out = os.path.join(self.folder, name)
            result = run_tool("stl", part, "--palette", palette, "--title", "Box",
                              "--tile-width", "60", "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(out, "rb") as handle:
                return handle.read()

        # Same palette, same file. Another palette, so the bytes differ.
        self.assertEqual(render("sea", "a.png"), render("sea", "b.png"))
        self.assertNotEqual(render("sea", "c.png"), render("sunset", "d.png"))

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_unknown_palette_stops_and_lists_the_names(self):
        part = os.path.join(self.folder, "part.stl")
        write_stl(part, box(0, 10, 0, 10, 0, 10))
        result = run_tool("stl", part, "--palette", "neon", "--title", "Box",
                          "--out", os.path.join(self.folder, "x.png"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rainbow, sunset, sea, meadow, berry, emerald, orchard, pine, straw", result.stderr)

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_palette_file_with_the_sea_colours_matches_the_sea_palette(self):
        part = os.path.join(self.folder, "part.stl")
        write_stl(part, box(0, 10, 0, 10, 0, 10))
        spec = os.path.join(self.folder, "sea.json")
        with open(spec, "w", encoding="utf-8") as handle:
            handle.write('{"anchors": ["#7FD3CF", "#9CC8EE", "#EED9A4"]}')

        def render(args, name):
            out = os.path.join(self.folder, name)
            result = run_tool("stl", part, *args, "--title", "Box", "--tile-width", "60", "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(out, "rb") as handle:
                return handle.read()

        self.assertEqual(render(["--palette-file", spec], "a.png"), render(["--palette", "sea"], "b.png"))

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_bad_palette_file_and_both_flags_stop(self):
        part = os.path.join(self.folder, "part.stl")
        write_stl(part, box(0, 10, 0, 10, 0, 10))
        spec = os.path.join(self.folder, "two.json")
        with open(spec, "w", encoding="utf-8") as handle:
            handle.write('{"anchors": ["#FFF", "#000"]}')
        out = os.path.join(self.folder, "x.png")

        bad = run_tool("stl", part, "--palette-file", spec, "--title", "Box", "--out", out)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn('"anchors": [3 hex colours]', bad.stderr)

        both = run_tool("stl", part, "--palette", "sea", "--palette-file", spec, "--title", "Box", "--out", out)
        self.assertNotEqual(both.returncode, 0)
        self.assertIn("not both", both.stderr)

    def test_missing_input_stops_with_message(self):
        result = run_tool("gif", os.path.join(self.folder, "nope.mp4"),
                          "--caption", "x", "--out", os.path.join(self.folder, "x.gif"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("input not found", result.stderr)

    def test_each_command_refuses_the_wrong_output_type(self):
        src = make_png(self.folder, "src.png", 64, 40, "green")
        before = make_png(self.folder, "before.png", 64, 40, "red")
        after = make_png(self.folder, "after.png", 64, 40, "blue")
        cases = [
            (["pair", before, after, "--caption", "OK"], "pair.gif", ".png"),
            (["gif", src, "--caption", "OK"], "demo.png", ".gif"),
            (["sheet", src, src, "--title", "OK"], "sheet.gif", ".png"),
        ]
        for command, name, ext in cases:
            with self.subTest(command=command[0]):
                out = os.path.join(self.folder, name)
                result = run_tool(*command, "--out", out)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"--out must end in {ext}", result.stderr)
                self.assertFalse(os.path.exists(out))

    def test_caption_longer_than_four_lines_stops_with_message(self):
        before = make_png(self.folder, "before.png", 64, 40, "red")
        after = make_png(self.folder, "after.png", 32, 40, "blue")
        out = os.path.join(self.folder, "pair.png")
        result = run_tool("pair", before, after, "--caption", "word " * 400, "--out", out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Shorten it", result.stderr)
        self.assertFalse(os.path.exists(out))

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_every_theme_renders_and_changes_the_sheet(self):
        part = os.path.join(self.folder, "part.stl")
        write_stl(part, box(0, 10, 0, 10, 0, 10))
        sheets = {}
        for name in ve.THEMES:
            with self.subTest(theme=name):
                out = os.path.join(self.folder, f"{name}.png")
                result = run_tool("stl", part, "--theme", name, "--title", "Box",
                                  "--tile-width", "60", "--out", out)
                self.assertEqual(result.returncode, 0, result.stderr)
                with open(out, "rb") as handle:
                    sheets[name] = handle.read()
        # Each theme changes the ground, ink, cut face or palette, so no two sheets match.
        self.assertEqual(len(set(sheets.values())), len(ve.THEMES))


class ThemeTests(unittest.TestCase):
    PALETTES = {"rainbow", "sunset", "sea", "meadow", "berry", "emerald", "orchard", "pine", "straw"}

    def test_every_theme_has_every_colour_and_a_known_palette(self):
        keys = {"canvas", "frame", "rule", "title", "caption", "label",
                "ground_centre", "ground_edge", "cut", "palette"}
        self.assertIn("paper", ve.THEMES)
        for name, theme in ve.THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(theme), keys)
                self.assertIn(theme["palette"], self.PALETTES)
                for key, value in theme.items():
                    if key != "palette":
                        self.assertRegex(value, r"^#[0-9a-fA-F]{6}$")


def box(x0, x1, y0, y1, z0, z1):
    """Twelve triangles for an axis-aligned box. Every face points outward."""
    faces = [
        ((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)),
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),
        ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
        ((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)),
        ((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)),
        ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),
    ]
    centre = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    tris = []
    for a, b, c, d in faces:
        for tri in ((a, b, c), (a, c, d)):
            u = [q - p for p, q in zip(tri[0], tri[1])]
            v = [q - p for p, q in zip(tri[0], tri[2])]
            normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            mid = [sum(axis) / 3 for axis in zip(*tri)]
            if sum(n * (m - c0) for n, m, c0 in zip(normal, mid, centre)) < 0:
                tri = (tri[0], tri[2], tri[1])
            tris.append(tri)
    return tris


# A long arm along +X at low Y, a tall arm along +Y at low X, and a tab on top
# of the long arm at high X. Each view then shows a known shape.
L_SOLID = box(0, 40, 0, 8, 0, 15) + box(0, 12, 8, 20, 0, 15) + box(30, 40, 0, 8, 15, 25)


def write_stl(path, triangles):
    with open(path, "wb") as handle:
        handle.write(b"test".ljust(80, b"\0"))
        handle.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            handle.write(struct.pack("<3f", 0, 0, 0))
            for vertex in tri:
                handle.write(struct.pack("<3f", *vertex))
            handle.write(b"\0\0")


def read_png(path):
    """Check every chunk CRC, inflate the pixels, and return (width, height, rows).

    The renderer writes filter type 0 only, so each row is raw RGB bytes.
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: bad PNG signature")
    pos, idat, width, height = 8, b"", 0, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if zlib.crc32(kind + body) != crc:
            raise ValueError(f"{path}: bad CRC in chunk {kind!r}")
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    if len(raw) != height * stride:
        raise ValueError(f"{path}: pixel data has the wrong length")
    rows = [raw[y * stride + 1:(y + 1) * stride] for y in range(height)]
    return width, height, rows


def pixel(rows, x, y):
    return rows[y][x * 3:x * 3 + 3]


def ground(x, y, size):
    """The renderer's ground colour at a pixel. It runs from GROUND_CENTRE to GROUND_EDGE."""
    t = min(1.0, math.hypot(x + 0.5 - size / 2, y + 0.5 - size / 2) / (size * math.sqrt(0.5)))
    s = t * t * (3 - 2 * t)
    return tuple(round(c + (e - c) * s) for c, e in zip(GROUND_CENTRE, GROUND_EDGE))


def is_ground(rows, x, y):
    """True when the pixel is the ground, within 2 levels per channel."""
    return all(abs(a - b) <= 2 for a, b in zip(pixel(rows, x, y), ground(x, y, len(rows))))


@unittest.skipUnless(HAVE_NODE, "needs node")
class RenderStlTests(unittest.TestCase):
    VIEWS = ["iso", "iso-cutaway", "plus-x", "minus-x", "plus-y", "minus-y", "plus-z", "minus-z"]

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.folder)
        stl = os.path.join(cls.folder, "L.stl")
        write_stl(stl, L_SOLID)
        cls.out = os.path.join(cls.folder, "out")
        result = subprocess.run(
            ["node", str(RENDER), stl, cls.out, "--size", "200"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)

    def test_writes_eight_valid_pngs_at_requested_size(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                width, height, _ = read_png(os.path.join(self.out, f"L-{view}.png"))
                self.assertEqual((width, height), (200, 200))

    def test_top_view_has_tall_arm_on_the_left(self):
        # Top view: +X points right, +Y points up. The tall arm is at low X and high Y.
        _, _, rows = read_png(os.path.join(self.out, "L-plus-z.png"))
        self.assertFalse(is_ground(rows, 40, 80))
        self.assertTrue(is_ground(rows, 160, 80))

    def test_front_view_has_tab_on_the_right(self):
        # Front view: +X points right, +Z points up. The tab is at high X and high Z.
        _, _, rows = read_png(os.path.join(self.out, "L-minus-y.png"))
        self.assertFalse(is_ground(rows, 164, 68))
        self.assertTrue(is_ground(rows, 40, 68))

    def test_ground_follows_the_centre_to_corner_gradient(self):
        _, _, rows = read_png(os.path.join(self.out, "L-plus-z.png"))
        self.assertTrue(is_ground(rows, 0, 0))
        self.assertTrue(is_ground(rows, 100, 2))
        self.assertTrue(is_ground(rows, 20, 180))

    def test_frame_keeps_one_scale_for_before_and_after(self):
        short = os.path.join(self.folder, "short.stl")
        tall = os.path.join(self.folder, "tall.stl")
        write_stl(short, box(0, 10, 0, 10, 0, 10))
        write_stl(tall, box(0, 10, 0, 10, 0, 20))

        def filled_rows(folder):
            # Count the rows that are not ground at column 100 of the front view.
            _, _, rows = read_png(os.path.join(folder, "short-minus-y.png"))
            return sum(1 for y in range(len(rows)) if not is_ground(rows, 100, y))

        alone = os.path.join(self.folder, "alone")
        framed = os.path.join(self.folder, "framed")
        for folder, extra in ((alone, []), (framed, ["--frame", tall])):
            subprocess.run(["node", str(RENDER), short, folder, "--size", "200", *extra],
                           check=True, capture_output=True)
        # Alone, the short part fills the frame. Framed with the tall part, it is
        # 10 of 20 units high, so about half of 200 px.
        self.assertGreater(filled_rows(alone), 160)
        self.assertTrue(80 <= filled_rows(framed) <= 92, filled_rows(framed))


def count_near(rows, colour, tolerance):
    """Count pixels whose red, green and blue are each within tolerance of colour."""
    return sum(
        1
        for row in rows
        for x in range(len(row) // 3)
        if all(abs(c - k) <= tolerance for c, k in zip(row[3 * x:3 * x + 3], colour))
    )


def count_darker(rows, shade, margin):
    """Count pixels whose average channel is more than margin below shade."""
    return sum(
        1
        for row in rows
        for x in range(len(row) // 3)
        if sum(row[3 * x:3 * x + 3]) / 3 < shade - margin
    )


@unittest.skipUnless(HAVE_NODE, "needs node")
class RenderLookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.folder)
        parts = {
            "cube": box(0, 10, 0, 10, 0, 10),
            "boss": box(0, 10, 0, 10, 0, 10) + box(4, 6, 4, 6, 10, 14),
        }
        for name, triangles in parts.items():
            stl = os.path.join(cls.folder, f"{name}.stl")
            write_stl(stl, triangles)
            result = subprocess.run(
                ["node", str(RENDER), stl, os.path.join(cls.folder, name), "--size", "200"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise AssertionError(result.stderr)

    def rows(self, name, view):
        return read_png(os.path.join(self.folder, name, f"{name}-{view}.png"))[2]

    def test_only_the_iso_cutaway_shows_the_cut_face(self):
        # The cube is cut through its centre, so the iso cutaway shows the inside of its far walls.
        self.assertEqual(count_near(self.rows("cube", "iso"), CUT_FACE, 10), 0)
        self.assertEqual(count_near(self.rows("cube", "plus-x"), CUT_FACE, 10), 0)
        self.assertGreater(count_near(self.rows("cube", "iso-cutaway"), CUT_FACE, 10), 300)

    def test_a_boss_darkens_the_floor_around_it(self):
        # Top view. The centre pixel is the boss top. A flat cube has no pixel darker than
        # its top. The floor beside the boss is in its shadow, so some pixels are darker.
        # Only the centre window is counted. The ink line on the cube outline is outside it.
        def darker(name):
            rows = self.rows(name, "plus-z")
            window = [row[60 * 3:140 * 3] for row in rows[60:140]]
            return count_darker(window, sum(pixel(rows, 100, 100)) / 3, 6)

        self.assertEqual(darker("cube"), 0)
        self.assertGreater(darker("boss"), 100)


if __name__ == "__main__":
    unittest.main()
