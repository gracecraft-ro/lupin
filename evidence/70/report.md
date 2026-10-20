# Issue 70: the doc editor threw away typed text with no warning

The page has an "auto-refresh" box that reloads the page every 10 seconds.
With the box ticked, the text in the doc editor was gone after one reload.
Nothing warned the person.

Before: the editor after a reload in the old build. The typed line
"IMPORTANT NOTE NOT SAVED" is gone.

After: the editor after 13 seconds in the new build, with the auto-refresh
box ticked and the typed line still there. The page did not reload.

## The old build

There was no `beforeunload` handler, so a reload or a link click went
straight through:

```js
// in the page, old build
new Event("beforeunload", {cancelable: true}) -> defaultPrevented: false
window.onbeforeunload -> null
```

After a reload the textarea held the saved text again. The typed line was
lost.

## The new build

The page now uses the browser's own leave-page warning. The handler runs
only when the textarea differs from the saved text:

```js
// with unsaved text
new Event("beforeunload", {cancelable: true}) -> defaultPrevented: true
```

The auto-refresh box waits while the editor holds unsaved text, so the
reload does not happen at all:

```js
// after 13 seconds with the auto-refresh box ticked, new build
{alive: "1", value: "# Lupin loop doc\n\nFirst line.\nSecond line.\nSecond line.\nIMPORTANT NOTE NOT SAVED"}
```

The `alive` marker survived, so the page did not reload, and the typed text
is still in the box.

## Why this browser behaviour

The browser already asks "Leave site? Changes you may not be saved." for a
reload or a link click, so the fix uses that prompt instead of a dialog of
our own. The prompt only appears when there is something to lose, so the
handler compares the textarea with the text that was saved. The auto-refresh
box is a reload the person did not ask for, so the reload waits while the
editor holds unsaved text. That keeps the typed text on screen instead of
asking a question every 10 seconds.

## How it was checked

Real Chromium, the real dashboard on `127.0.0.1:8901`, the text typed into
the textarea, and the page state read back after 13 seconds with the
auto-refresh box ticked.
