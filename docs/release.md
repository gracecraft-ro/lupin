# Release Lupin

## One version source

`pyproject.toml` holds the version. `flake.nix` reads it:

```nix
version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
```

Do not write the version in another file. `lupin --version` reads the
installed package metadata, or `pyproject.toml` in a source checkout.

## Before a release

1. Merge every change into `main`.
2. Run the gate on `main`:

   ```sh
   nix flake check
   nix develop -c python3 -m pytest -v
   ```

## Cut the release

1. Set the new version in `pyproject.toml`. For example `0.2.0`.
2. Commit the version change:

   ```sh
   git add pyproject.toml
   git commit -m "release: v0.2.0"
   ```
3. Push the release commit to `main`, or merge the release pull request.
   A tag must point at a commit on `main`.
4. Tag that commit and push the tag:

   ```sh
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```
5. Check the tag runs:

   ```sh
   nix run github:OWNER/REPO/v0.2.0 -- --version
   ```

## Rules

- A tag is `v` and the version from `pyproject.toml`.
- Do not move or delete a tag.
- A security fix gets a patch version.
