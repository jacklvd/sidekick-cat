"""Post the welcome comment on PR open (no AI)."""

import os
from pathlib import Path

from scripts import gh

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "welcome.md"


def run(repo, pr_number, author):
    """Post the welcome comment and assign the opener. Host-agnostic core."""
    body = TEMPLATE.read_text(encoding="utf-8").format(author=author)
    gh.upsert_comment(repo, pr_number, "bot:welcome", body)
    gh.assign(repo, pr_number, author)  # auto-assign the PR opener


def main():
    run(gh.get_repo(), int(os.environ["PR_NUMBER"]), os.environ["PR_AUTHOR"])


if __name__ == "__main__":
    main()
