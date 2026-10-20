# Issue 65: the Repos page dropped the loop platform of every repo

The schedule file has two columns: repo name and loop platform. The Repos
page wrote the file again with only the repo name, so every repo went back
to `claude`.

Before: the schedule file after one click on "Add to schedule" in the old
build. The platform column is gone.

```
lupin
newrepo
widgets
```

After: the same click in the new build. The platform column stays.

```
lupin omp
widgets claude
newrepo claude
```

Both images show the file on disk, read in the browser after the click.

## The schedule file, before the fix

The file started with two columns:

```
lupin omp
widgets claude
```

After one click on "Add to schedule" in the old build:

```
lupin
newrepo
widgets
```

Every platform was gone. The loops went back to `claude` with nothing on
screen to say so.

## The schedule file, after the fix

The same click in the new build:

```
lupin omp
widgets claude
newrepo claude
```

The platform column stays. The new repo gets the default platform.

## How it was checked

Real Chromium, the real dashboard on `127.0.0.1:8901`, a real click on the
button, and the file read back from disk after the click.
