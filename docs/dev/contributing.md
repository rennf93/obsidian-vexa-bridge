# Contributing

Thanks for considering a contribution. The full guide is in [CONTRIBUTING.md](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/CONTRIBUTING.md); the essentials are below.

## Dev install

**Python 3.11 is required** (pinned for parity with `discord-vexa-bridge`). [uv](https://github.com/astral-sh/uv) is the package manager.

```bash
uv sync --extra dev     # install runtime + dev deps into .venv
make test               # pytest
make lint               # ruff lint + format check
make fix                # auto-fix
make typecheck          # mypy (must stay clean)
make build              # build the Docker image
```

## Gates (must pass before a PR)

```bash
make lint          # ruff check + ruff format --check
make typecheck     # mypy summarizer
make test          # pytest
make pre-commit    # pre-commit run --all-files (ruff, ruff-format, mypy, bandit)
```

CI (`.github/workflows/ci.yml`) runs the same. `mypy summarizer` is clean and gated — prefer narrowing/guards over `# type: ignore`.

## Pull requests

- Fill in the PR template.
- Add/adjust tests for behavior changes.
- Update the README/docs and `CHANGELOG.md` (`## [Unreleased]`) for user-facing changes.
- Never commit secrets (`VEXA_API_KEY`, `AI_API_KEY`, `OBSIDIAN_MCP_TOKEN`, `.env`).

## CLA

Inbound contributions are accepted under the [Contributor License Agreement](https://github.com/rennf93/obsidian-vexa-bridge/blob/master/CLA.md). The CLA grants the maintainer a broad copyright license **including the right to sublicense**, which is what lets the project be offered under both AGPL-3.0-or-later and a commercial license. Post the sign-off comment on your PR; the CLA Assistant bot records it on the `cla-signatures` branch.