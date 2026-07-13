# Releasing

The project uses [Semantic Versioning](https://semver.org/). A release is a GitHub Release with a `vX.Y.Z` tag; CI builds and pushes the image.

## Cut a release

1. Update [`CHANGELOG.md`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/CHANGELOG.md) — move `## [Unreleased]` items under a new `## [X.Y.Z] - YYYY-MM-DD` heading.
2. Bump `version` in [`pyproject.toml`](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/pyproject.toml). The helper at `.github/scripts/bump_version.py` does both (`python .github/scripts/bump_version.py X.Y.Z`).
3. Commit, then create a GitHub Release with the `vX.Y.Z` tag.

## What CI does on release

`.github/workflows/release.yml` fires on `release: published`. It:

- Builds the image once, tagging for **GHCR** (`ghcr.io/rennf93/obsidian-vexa-bridge`) and **Docker Hub** (`docker.io/renzof93/obsidian-vexa-bridge`) at `:VERSION` and `:latest`.
- Pushes only after the build succeeds (no half-publish).
- Labels the image with `org.opencontainers.image.licenses=AGPL-3.0-or-later` and `org.opencontainers.image.source`.

The same workflow runs on `workflow_dispatch` for a manual build (uses the short SHA as the tag).

## Docker image

The prebuilt image is `renzof93/obsidian-vexa-bridge:latest` (Docker Hub) / `ghcr.io/rennf93/obsidian-vexa-bridge:latest`. The `compose-snippet.yml` uses `renzof93/obsidian-vexa-bridge:latest`. To build from a local checkout instead, use the `build:` block commented in that snippet.