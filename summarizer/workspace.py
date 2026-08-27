"""The workspace template (conventions for Vexa's agent) and the one-time init helper.

The template is data shipped next to the package (workspace-template/ at the repo root, copied
into the image by the Dockerfile). init_workspace copies it into a local clone of the user's
repo without overwriting anything they already have; setup_instructions prints the Vexa calls
that attach that repo as the workspace, with the token left as a placeholder so it never
appears in a shell history or a log.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "workspace-template"


def init_workspace(target: Path) -> list[Path]:
    """Copy every template file missing from target; return the paths written. Never overwrites."""
    written: list[Path] = []
    for src in sorted(TEMPLATE_DIR.rglob("*")):
        if not src.is_file():
            continue
        dst = target / src.relative_to(TEMPLATE_DIR)
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        written.append(dst)
    return written


def setup_instructions(api_base: str, repo_url: str) -> str:
    base = api_base.rstrip("/")
    return "\n".join(
        [
            "Next steps (run from a machine that can reach your Vexa gateway; replace <PAT> with a GitHub",
            "personal access token that can read and write the repo; the token is stored by Vexa once,",
            "server-side, never in the repo):",
            "",
            "1. Commit and push the template to the repo's default branch:",
            "   git add -A && git commit -m 'Add Vexa knowledge workspace conventions' && git push",
            "",
            "2. Save the token once:",
            f"   curl -X POST '{base}/agent/workspace/git-token' -H 'X-API-Key: $VEXA_API_KEY' \\",
            "        -H 'Content-Type: application/json' -d '{\"token\":\"<PAT>\"}'",
            "",
            "3. Attach the repo as your Vexa workspace:",
            f"   curl -X POST '{base}/agent/workspace/swap' -H 'X-API-Key: $VEXA_API_KEY' \\",
            f'        -H \'Content-Type: application/json\' -d \'{{"repo":"{repo_url}","ref":"main"}}\'',
            "",
            "4. Confirm CLAUDE.md is at the workspace root (not wrapped under kg/<repo>/):",
            f"   curl '{base}/agent/workspace/tree' -H 'X-API-Key: $VEXA_API_KEY'",
            "",
            "5. Clone the same repo into your vault as the mirror folder, then start the bridge with",
            "   BRIDGE_MODE=graph. The bridge creates the fold routine on its first pass.",
        ]
    )
