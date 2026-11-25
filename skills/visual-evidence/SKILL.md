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
- Run the `stl` command in Scripts, below. It renders all six sides and the iso
  view, and it labels each tile. With `--before`, the before and after tiles use
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
  sides and the iso view. Before and after must use the same scale. A close-up
  is needed when the change is too small to see at full size.

## Scripts

The scripts are in `scripts/`, next to this file. Each one prints the path of
the file it wrote, its size, and its width and height. Open the file and check
it. The scripts do not judge the picture.

- `scripts/visual_evidence.py stl` renders a binary STL to six sides and the iso
  view, then tiles them with labels. With `--before`, it adds a BEFORE row above
  an AFTER row, at one scale. It needs ffmpeg and node.

  ```sh
  nix shell nixpkgs#ffmpeg nixpkgs#nodejs -c python3 scripts/visual_evidence.py stl \
    part-after.stl --before part-before.stl --title "Part, before and after" --out sheet.png
  ```

- `scripts/visual_evidence.py pair` puts before and after side by side, with a
  `Before` and `After` label. It needs ffmpeg.

  ```sh
  nix shell nixpkgs#ffmpeg -c python3 scripts/visual_evidence.py pair \
    before.png after.png --caption "Save button moves to the bottom bar" --out pair.png
  ```

- `scripts/visual_evidence.py gif` adds a caption bar to a video or gif. It needs
  ffmpeg.

  ```sh
  nix shell nixpkgs#ffmpeg -c python3 scripts/visual_evidence.py gif \
    demo.mp4 --caption "Tap Save to confirm" --out demo.gif
  ```

- `scripts/visual_evidence.py sheet` tiles any images you name, with labels and a
  title. It needs ffmpeg.

  ```sh
  nix shell nixpkgs#ffmpeg -c python3 scripts/visual_evidence.py sheet \
    top.png="Top" front.png="Front" --title "Part A, same views" --out sheet.png
  ```

- `scripts/render_stl.mjs` renders one binary STL to seven PNG files: `iso`,
  `plus-x`, `minus-x`, `plus-y`, `minus-y`, `plus-z`, and `minus-z`. The `stl`
  command calls it. Run it directly only for a plain render. Its `--frame
  OTHER.stl` option sets the scale from both files. It does not read ASCII STL.
  It draws one grey part under one light, so it shows shape, not colour or text.
