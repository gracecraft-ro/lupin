"""Tests for the visual-evidence skill scripts.

The ffmpeg tests skip without ffmpeg and ffprobe. The render tests skip
without node.
"""

import importlib.util
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
WHITE = b"\xff\xff\xff"

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


class WrapTests(unittest.TestCase):
    def test_wrap_breaks_long_text_and_keeps_newlines(self):
        lines = ve.wrap("Save button moves to the bottom bar, so it stays visible on phones", 200, 22)
        per_line = int(200 / (22 * ve.CHAR_EM))
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= per_line for line in lines))
        self.assertEqual(ve.wrap("one\ntwo", 1000, 22), ["one", "two"])


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
        # Panels 64 + 32 wide. Label band 40 + panel 40. One caption line: 2*8 + 22+6 = 44.
        self.assertEqual(ve.probe(out), (96, 124))

    def test_gif_keeps_width_and_adds_caption_band(self):
        src = make_png(self.folder, "src.png", 240, 120, "green")
        out = os.path.join(self.folder, "demo.gif")
        result = run_tool("gif", src, "--caption", "Tap Save", "--width", "120", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # 120 wide scales to 60 high. One caption line at 18 px: 2*8 + 24 = 40.
        self.assertEqual(ve.probe(out), (120, 100))

    def test_sheet_grid_and_title_band(self):
        views = [make_png(self.folder, f"v{i}.png", 60, 40, "gray") for i in range(4)]
        out = os.path.join(self.folder, "sheet.png")
        result = run_tool("sheet", *views, "--title", "Part", "--cols", "2",
                          "--tile-width", "60", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Two by two tiles of 60 x 40. Title band at 24 px: 2*8 + 24+6 = 46.
        self.assertEqual(ve.probe(out), (120, 126))

    @unittest.skipUnless(HAVE_NODE, "needs node")
    def test_stl_before_and_after_sheet_is_two_rows_of_seven(self):
        before = os.path.join(self.folder, "before.stl")
        after = os.path.join(self.folder, "after.stl")
        write_stl(before, box(0, 10, 0, 10, 0, 10))
        write_stl(after, box(0, 10, 0, 10, 0, 20))
        out = os.path.join(self.folder, "stl.png")
        result = run_tool("stl", after, "--before", before, "--title", "Box",
                          "--tile-width", "120", "--out", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Seven 120 px tiles across, two rows of 120 px, and a 46 px title band.
        self.assertEqual(ve.probe(out), (840, 286))

    def test_missing_input_stops_with_message(self):
        result = run_tool("gif", os.path.join(self.folder, "nope.mp4"),
                          "--caption", "x", "--out", os.path.join(self.folder, "x.gif"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("input not found", result.stderr)


def box(x0, x1, y0, y1, z0, z1):
    """Twelve triangles for an axis-aligned box."""
    faces = [
        ((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)),
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)),
        ((x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)),
        ((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)),
        ((x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)),
        ((x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)),
    ]
    tris = []
    for a, b, c, d in faces:
        tris += [(a, b, c), (a, c, d)]
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


@unittest.skipUnless(HAVE_NODE, "needs node")
class RenderStlTests(unittest.TestCase):
    VIEWS = ["iso", "plus-x", "minus-x", "plus-y", "minus-y", "plus-z", "minus-z"]

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

    def test_writes_seven_valid_pngs_at_requested_size(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                width, height, _ = read_png(os.path.join(self.out, f"L-{view}.png"))
                self.assertEqual((width, height), (200, 200))

    def test_top_view_has_tall_arm_on_the_left(self):
        # Top view: +X points right, +Y points up. The tall arm is at low X and high Y.
        _, _, rows = read_png(os.path.join(self.out, "L-plus-z.png"))
        self.assertNotEqual(pixel(rows, 40, 80), WHITE)
        self.assertEqual(pixel(rows, 160, 80), WHITE)

    def test_front_view_has_tab_on_the_right(self):
        # Front view: +X points right, +Z points up. The tab is at high X and high Z.
        _, _, rows = read_png(os.path.join(self.out, "L-minus-y.png"))
        self.assertNotEqual(pixel(rows, 164, 68), WHITE)
        self.assertEqual(pixel(rows, 40, 68), WHITE)

    def test_frame_keeps_one_scale_for_before_and_after(self):
        short = os.path.join(self.folder, "short.stl")
        tall = os.path.join(self.folder, "tall.stl")
        write_stl(short, box(0, 10, 0, 10, 0, 10))
        write_stl(tall, box(0, 10, 0, 10, 0, 20))

        def filled_rows(folder):
            # Count the rows that are not white at column 100 of the front view.
            _, _, rows = read_png(os.path.join(folder, "short-minus-y.png"))
            return sum(1 for row in rows if row[300:303] != WHITE)

        alone = os.path.join(self.folder, "alone")
        framed = os.path.join(self.folder, "framed")
        for folder, extra in ((alone, []), (framed, ["--frame", tall])):
            subprocess.run(["node", str(RENDER), short, folder, "--size", "200", *extra],
                           check=True, capture_output=True)
        # Alone, the short part fills the frame. Framed with the tall part, it is
        # 10 of 20 units high, so about half of 200 px.
        self.assertGreater(filled_rows(alone), 160)
        self.assertTrue(80 <= filled_rows(framed) <= 92, filled_rows(framed))


if __name__ == "__main__":
    unittest.main()
