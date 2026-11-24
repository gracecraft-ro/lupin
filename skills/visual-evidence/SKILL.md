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

| Change | Evidence |
| --- | --- |
| Appearance only. Users do the same steps. Example: a button color changes. | Screenshots, before and after. |
| Users do something new. Example: a new control, a new step, a new gesture. | A gif that shows the new behavior. |
| New screen | A gif that shows how a user reaches the screen, what the user does on it, and what happens next. |
| Spatial object: a 3D model, an STL, a CAD part, or code that makes one | A contact sheet (section 5). |

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
shell has ffmpeg, ffprobe, and node. Outside a dev shell, use
`nix shell nixpkgs#ffmpeg nixpkgs#nodejs -c` in place of `nix develop -c`.

Each command prints one line: `wrote PATH WIDTHxHEIGHT bytes=N`. Open the file
and check it. The scripts do not judge the picture.

### Rules for every command

- `--out` must end in the type the command writes. `pair`, `sheet`, and `stl`
  write `.png`. `gif` writes `.gif`. Another name stops the command before it
  writes a file.
- Text is measured with the font that draws it. A word too wide for one line is
  cut at the line edge, and the rest goes on the next line.
- A caption or title can be at most 4 lines. Longer text stops the command with
  `text needs more than 4 lines at this width. Shorten it.` Shorten the text.
  Do not make the font smaller.
- `--caption` is for `pair` and `gif`. A caption says what changed.
  `--title` is for `sheet` and `stl`. A title names the whole picture. Keep
  them separate.
- Placement: `pair` puts the caption at the top, above the Before and After
  labels. `gif` puts the caption at the bottom. `sheet` and `stl` put the title
  at the top.

| Command | Input | Writes | Text | Other options (default) |
| --- | --- | --- | --- | --- |
| `pair` | Two PNG screenshots | `.png` | `--caption` | `--size` caption px (22) |
| `gif` | A video or gif | `.gif` | `--caption` | `--width` px (480), `--size` caption px (18), `--fps` (10) |
| `sheet` | Two or more PNG images | `.png` | `--title` | `--cols` (3), `--tile-width` px (480) |
| `stl` | A binary STL. `--before` takes the old STL. | `.png` | `--title` | `--tile-width` px (240) |

Every command takes `--font`, a font name or a `.ttf` path. The default is
DejaVu Sans.

### Same screen, before and after (screenshots)

1. Take one screenshot before the change and one after. Use the same window
   size, theme, data, and state.
2. Run:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py pair \
     before.png after.png --caption "Save button moves to the bottom bar" --out pair.png
   ```

Each panel keeps its own size. The shorter one gets white space below it, so
nothing is scaled. The panels sit 16 px apart.

### New behaviour or new screen (gif)

1. Record the steps as a video or a gif. The whole input becomes frames, so
   keep the recording short.
2. Run:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py gif \
     demo.mp4 --caption "Tap Save to confirm" --out demo.gif
   ```

`pair` takes still images only. To show before and after as gifs, run `gif`
twice. Name the files `before.gif` and `after.gif`. Attach both (section 3).

### Spatial object (STL)

1. Export the part as a binary STL. The renderer does not read ASCII STL.
2. For a change, pass the old file with `--before`:

   ```sh
   nix develop -c python3 $SKILL/scripts/visual_evidence.py stl \
     part-after.stl --before part-before.stl --title "Part, before and after" --out sheet.png
   ```

   With `--before`, the sheet has two rows of eight tiles. The first row is
   BEFORE and the second is AFTER. Each row shows ISO, ISO CUT, +X, -X, +Y, -Y,
   +Z, and -Z, in that order. ISO CUT removes the half nearest the camera. The
   inside of the far walls shows in orange, so wall thickness is visible. Both rows use one scale, so a size change shows.
   Without `--before`, the sheet has one row.
3. For a close-up, `stl` does not make one. Render the view larger, then crop
   it. `crop=WIDTH:HEIGHT:X:Y` counts pixels from the top-left corner:

   ```sh
   nix develop -c node $SKILL/scripts/render_stl.mjs part-after.stl closeup --size 2400
   nix develop -c ffmpeg -i closeup/part-after-plus-z.png -vf crop=600:400:900:700 closeup.png
   ```

   Open the crop and check that it shows the changed area. The renderer lights the part
   from several sides and darkens crevices. Small detail such as an engraving can
   still be hard to see.
   Say so in the report if the detail does not show.

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

