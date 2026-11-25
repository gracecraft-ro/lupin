---
name: visual-evidence
description: >-
  Rules for visual proof in a pull request. Covers which changes need evidence,
  what to capture (screenshot, gif, or contact sheet), how to caption and compare
  before and after, and how to review the evidence. Use when a change affects
  anything a person can see, when you capture evidence for a pull request, and
  when you review that evidence.
compatibility: Needs the repo's own capture tools. The upload steps are in the ship skill.
---

# Visual evidence

Use this skill with `ship` (upload steps) and `code-review` (review checks).

If the repo has screenshot or gif guidance, read it first. Use it as the
starting point. If it is stricter than this skill, follow the repo guidance.

## 1. Decide if the change is visual

- A visual change is a change a person can see.
- Open the real surface. Do not use a mock-up, a source file, or a test result
  as proof.
- If you decide a change is not visual, say why. Write the reason in your
  report and in a pull request comment.
- If you cannot open the surface, say so in your report. Do not claim that it
  works.

## 2. Pick the format

| Change                                                                                                                                     | Evidence                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Appearance only. Users do the same steps. Example: a button color changes.                                                                 | Screenshots, before and after.                                                                   |
| Users do something new, or the change adds an animation or a new flow. Example: a new control, a new step, a new gesture, a new animation. | A gif that shows the new behavior or animation.                                                  |
| New screen                                                                                                                                 | A gif that shows how a user reaches the screen, what the user does on it, and what happens next. |
| Spatial object: a 3D model, an STL, a CAD part, or code that makes one                                                                     | A contact sheet (section 5).                                                                     |
| Drawing file (SVG or DXF) that code writes. Example: a cut file changes.                                                                   | A drawing sheet (`drawing` command).                                                             |
| PDF that code writes. Example: a print template changes.                                                                                   | A PDF page sheet (`pdf` command).                                                                |

## 3. Capture before and after

- Capture the same view before and after. Use the same window size, theme,
  data, and state.
- A new screen has no before. Capture the new screen.
- Show before and after side by side in one image.
- For a gif, show before and after side by side in one file when you can.
  Otherwise, attach two gifs. Name one `before` and one `after`.

## 4. Caption the evidence

- Strongly prefer images with a baked-in caption. A baked-in caption is text
  drawn on the image itself. It stays with the image when the image is copied.
- Label each panel `Before` or `After`.
- Add one sentence that says what the picture shows and what changed.
- Write the caption so a reader who did not see the change can understand it.

## 5. Contact sheet for spatial objects

- Make a contact sheet for any change to code that generates a spatial object.
  Do this even when the change looks small. This includes a change that adds a
  feature to that code, or fixes it.
- Run the `stl` command in Scripts, below. It renders all six sides, the iso view,
  and the iso cutaway, and it labels each tile. With `--before`, the before and after tiles use
  one scale, so a size change shows. You do not pick the views.
- If the object is not a binary STL, export it to binary STL first. Then run the
  command on the export.
- Add a close-up of the changed area when the change is too small to see at full
  size. The `stl` command does not make close-ups. Make the close-up by hand.
- If the change involves several related objects, include each reference
  object. Example: a new clip made for an existing body. Show the existing body.
- Attach the sheet to the pull request.

## 6. Check each file before upload

- Open each file and look at all of it.
- Check that it shows the change. Check that the caption is correct. Check that
  the result looks finished.
- Capture again if a file is wrong or unclear. Do not upload a bad file.
- Remove private data from the image. This includes names, emails, tokens, and
  real customer data.
- Keep evidence files out of commits. Commit them only when the repo guidance
  says to.

## 7. Report

- Attach each file to the pull request. Do not give only a local path. The
  reader may not be able to open a scratch folder or a `/tmp` path.
- Post each visual change to the pull request when it happens. Use one comment
  per change. Say what changed and why.
- List each file and what it shows.
- State what you did not capture, and why.

## 8. Review

- A visual pull request without evidence is not approved.
- Look at each image closely. Check that it shows the change, that the caption
  is correct, and that the result has no visual bug.
- For a new screen, check that the gif covers how the screen is reached, how it
  is used, and what happens after.
- For a spatial-object change, check the contact sheet. It must have the six
  sides, the iso view, and the iso cutaway. Before and after must use the same scale. A close-up
  is needed when the change is too small to see at full size.

## Scripts

The scripts are in `scripts/`, next to this file. In the examples, `$SKILL` is
this skill's folder. In the lupin repo it is `skills/visual-evidence`. Run the
examples from the repo root, in the repo's dev shell (`nix develop -c`). The
shell has ffmpeg, ffprobe, node, fontconfig (fc-match), the DejaVu faces, rsvg-convert (librsvg), and pdftoppm (poppler).

Each command prints one line: `wrote PATH WIDTHxHEIGHT bytes=N`. Open the file
and check it. The scripts do not judge the picture. Then attach the file to the
pull request (section 7).

### Rules for every command

- `--out` must end in the type the command writes. `pair`, `sheet`, `stl`,
  `drawing`, and `pdf` write `.png`. `gif` writes `.gif`. Another name stops the command before it
  writes a file.
- Text is measured with the font that draws it. A word too wide for one line is
  cut at the line edge, and the rest goes on the next line.
- A caption or title can be at most 4 lines. Longer text stops the command with
  `text needs more than 4 lines at this width. Shorten it.` Shorten the text.
  Do not make the font smaller.
- `--caption` is for `pair` and `gif`. A caption says what changed.
  `--title` is for `pair`, `sheet`, `stl`, `drawing`, and `pdf`. A title names the whole picture.
  Keep them separate.
- Placement: `pair` puts the title at the top, then the caption, then the Before
  and After labels. `gif` puts the caption at the bottom. `sheet`, `stl`, `drawing`, and
  `pdf` put the title at the top. A title has a hairline under it. Labels sit above their
  image, never on it.
- Text sizes are fixed in every command: title 44 px, caption 34 px, label 24 px.
  `--size` sets the caption size only.
- `--theme NAME` sets the ground, frame, ink, cut face, and default palette in every
  command. Names: `paper` (default), `roundsmith` (white UI), `roundsmith-night`
  (dark UI), `roundsmith-band` (marketing dark band), and `roundsmith-straw` (straw
  sheet). The colours are in `scripts/themes.json`.

| Command   | Input                                              | Writes | Text                              | Other options (default)                                                        |
| --------- | -------------------------------------------------- | ------ | --------------------------------- | ------------------------------------------------------------------------------ |
| `pair`    | Two PNG screenshots                                | `.png` | `--title` (optional), `--caption` | `--crop W:H:X:Y` px, applied to both panels. `--size` caption px (34)          |
| `gif`     | A video or gif                                     | `.gif` | `--caption`                       | `--width` px (480), `--size` caption px (34), `--fps` (10)                     |
| `sheet`   | Two or more PNG images                             | `.png` | `--title`                         | `--cols` (3), `--tile-width` px (the first image's width)                      |
| `stl`     | A binary STL. `--before` takes the old STL.        | `.png` | `--title`                         | `--tile-width` px (240), `--palette` NAME (`rainbow`) or `--palette-file` FILE |
| `drawing` | An SVG or DXF file. `--before` takes the old file. | `.png` | `--title`                         | `--tile-width` px (800)                                                        |
| `pdf`     | A PDF. `--before` takes the old PDF.               | `.png` | `--title`                         | `--tile-width` px (600)                                                        |

Titles use DejaVu Serif Bold, captions DejaVu Serif, and labels DejaVu Sans Mono Bold.
Every command takes `--font` to set one face for all text: a fontconfig name, or
a `.ttf` path.

### Same screen, before and after (screenshots)

1. Take one screenshot before the change and one after. Use the same window
   size, theme, data, and state.
2. Run:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py pair \
     before.png after.png --caption "Save button moves to the bottom bar" --out pair.png
   ```

Each panel keeps its own size. The shorter one gets space below it, so nothing
is scaled. The panels sit 32 px apart.

For a close-up of a small area, add `--crop`. It takes `W:H:X:Y` in px, counted
from the top-left corner, and applies the same crop to both panels:

```sh
nix develop -c python3 $SKILL/scripts/visual_evidence.py pair \
  before.png after.png --title "Save button, close-up" --crop 600:400:900:700 \
  --caption "The save button moves to the bottom bar" --out pair.png
```

The crop must lie inside the image. Otherwise the command stops with
`--crop is outside the WIDTHxHEIGHT image`.

### New behaviour or new screen (gif)

1. Record the steps as a video or a gif. The whole input becomes frames, so
   keep the recording short.
2. Run:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py gif \
     demo.mp4 --caption "Tap Save to confirm" --out demo.gif
   ```

The `gif` command runs ffmpeg. It writes frames at `--fps`, builds a palette
from them, and writes the gif with that palette.

`pair` takes still images only. To show before and after as gifs, run `gif`
twice. Name the files `before.gif` and `after.gif`. Attach both (section 3).

### Spatial object (STL)

1. Export the part as a binary STL. The renderer does not read ASCII STL, SVG,
   or DXF. It ignores layers and other file metadata.
2. For a change, pass the old file with `--before`:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py stl \
     part-after.stl --before part-before.stl --title "Part, before and after" --out sheet.png
   ```

   With `--before`, the sheet has two rows of eight tiles. Each row has its own
   heading, Before or After, in the title face with a hairline under it. Each row shows ISO, ISO CUT, +X, -X, +Y, -Y,
   +Z, and -Z, in that order. ISO CUT removes the half nearest the camera. The
   inside of the far walls shows in orange, so wall thickness is visible. Both rows use one scale, so a size change shows.
   Without `--before`, the sheet has one row.
   `--palette NAME` picks a colour set. Without it, the theme's default palette is used
   (set per theme in `scripts/themes.json`): `rainbow`, `sunset`, `sea`, `meadow`,
   `berry`, `emerald`, `orchard`, `pine`, or `straw`. `--palette-file FILE` takes your own colours:
   JSON with three hex `anchors` and an optional `base`. Use a different palette for each
   review round, and put its name in the title, so rounds look different.

3. For a close-up, `stl` does not make one. Render the view larger, then crop
   it. `crop=WIDTH:HEIGHT:X:Y` counts pixels from the top-left corner:

   ```sh
   nix develop -c node $SKILL/scripts/render_stl.mjs part-after.stl closeup --size 2400
   nix develop -c ffmpeg -i closeup/part-after-plus-z.png -vf crop=600:400:900:700 closeup.png
   ```

   Open the crop and check that it shows the changed area. The renderer lights the part
   from several sides, darkens crevices, and draws a thin ink line where a surface
   meets a farther one. It tints each part of the surface with a colour from the
   chosen palette, by its place in the part, so a spot keeps its colour from every view. Small detail such as an engraving can still be hard to see.
   Say so in the report if the detail does not show.

### Drawing (SVG or DXF)

Use this for a 2D drawing that code writes, such as a cut file.

1. Give the command the new file. For a change, also pass the old file with
   `--before`:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py drawing \
     cut-after.dxf --before cut-before.dxf --title "Cut pattern, before and after" --out drawing.png
   ```

- Both files use one scale. A size change shows.
- The file must end in `.svg` or `.dxf`. Another type stops the command.
- An SVG needs a `viewBox`. Without one, the command stops.
- A DXF can contain only `LINE` and `LWPOLYLINE` entities. Another entity, such
  as an arc or a circle, stops the command and names it.
- DXF layers are drawn as follows:
  - `CUT`: solid, in the title colour.
  - `SEAM`: dashed, in the caption colour.
  - `FOLD`: dotted, in the label colour.
  - `LABELS`: solid, in the cut colour.
  - Other layers, such as `ENGRAVE`: solid, in the caption colour.
- Roundsmith writes these files with `writeDxf` in `src/core/export/dxf.ts`.
- Without `--before`, the sheet has one tile. The tile is labeled with the file name.

### Mark a curve or part (drawing)

Use `--mark` to explain one curve or part of a drawing. Each mark adds three things to the after tile:

- a dot on the curve,
- a line from the dot to a badge,
- a number in the badge.

The legend under the sheet has one line for each mark. The line gives the number and your text.

1. Pick a point on the curve. Use an end point, or a point on the path. A control point of a Bezier curve is not on the curve.
2. Give X and Y in the drawing's own units. For a DXF, Y points up, as it does in the DXF file.

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py drawing \
     cut-after.svg --title "Cut pattern" \
     --mark 35 35 "Dashed S-curve. Seam path, left edge to right edge." \
     --mark 50 47.5 "Lower arc. Fold line." --out drawing.png
   ```

3. Open the picture. Check that each dot sits on its curve. If a dot is off, fix the coordinate and run the command again.

Rules for marks:

- Marks go on the after drawing only. The before tile has no marks.
- Each mark point must be inside the drawing. Otherwise the command stops and prints the point.
- A badge can sit past the drawing edge. The view grows so the badge fits. Both tiles use the grown view.
- Each legend line is at most 4 lines long, the same as a caption.
- The mark numbers use DejaVu Sans Mono bold. `--font` does not change them.
- The legend uses the caption size (34 px).

### PDF pages

Use this for a PDF that code writes, such as a print template.

1. Give the command the new PDF. For a change, also pass the old PDF with
   `--before`:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py pdf \
     print-after.pdf --before print-before.pdf --title "Print template, before and after" --out pdf.png
   ```

- The command draws each page at 150 dpi. Each page is one tile, labeled
  `Page 1`, `Page 2`, and so on.
- Before and after need the same page count. Otherwise the command stops and
  shows both counts.
- Roundsmith keeps each page at its own size. The command pads each page to the
  largest page size, from the top-left corner. So every page keeps its true
  scale. Empty space at the right or bottom of a smaller page is padding, not
  content.
- Each PDF is one row, with the heading Before or After. Without `--before`, the
  sheet has one row and no heading.

### Several views that are not STL (sheet)

Use `sheet` for PNG images you made yourself. Each `PATH=LABEL` sets a tile
label:

```sh
nix develop -c python3 $SKILL/scripts/visual_evidence.py sheet \
  top.png="Top" front.png="Front" --title "Part A, same views" --out sheet.png
```

### Direct render (render_stl.mjs)

`$SKILL/scripts/render_stl.mjs FILE.stl OUTDIR` writes eight PNG files: `iso`,
`iso-cutaway`, `plus-x`, `minus-x`, `plus-y`, `minus-y`, `plus-z`, and `minus-z`. Use it only
for a plain render or a close-up (see the STL section). `--size` sets the view
width in px (default 800). `--frame OTHER.stl` sets one scale from both files.
