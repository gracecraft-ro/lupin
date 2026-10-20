# Issue 66: saving a doc turned every line ending into CRLF

The doc editor sent the text of the textarea with CRLF line endings, and the
server wrote the body as it came. Every line of the file changed.

Before: the bytes of the doc file after one save in the old build. Every
line ends with `^M$`, which is CRLF.

After: the same save in the new build. The lines end with `$`, which is LF.

Both images show `cat -A` of the same file, so the change is in the bytes
of the file, not on the page.

## The file before the save

The file had LF endings:

```
$ cat -A docs/delegation-loop.md
# Lupin loop doc$
$
First line.$
Second line.$
```

## The file after the save, old build

The same file after one click on "Save", with no text changed:

```
$ cat -A docs/delegation-loop.md
# Lupin loop doc^M$
^M$
First line.^M$
Second line.^M$
```

Every line ending is CRLF now. In git the whole file shows as changed.

## The file after the save, new build

The same click on "Save" in the new build:

```
$ cat -A docs/delegation-loop.md
# Lupin loop doc$
$
First line.$
Second line.$
```

The file keeps the endings it had. A file that already uses CRLF keeps CRLF.

## How it was checked

Real Chromium, the real dashboard on `127.0.0.1:8901`, a real click on
"Save", and the file read back as bytes after the save.
