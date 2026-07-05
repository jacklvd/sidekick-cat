"""Check the PR description for required sections, post pass/fail (no AI)."""

import os
import re

from scripts import gh
from scripts.config import REQUIRED_SECTIONS


def missing_sections(body, sections):
    # Match a section only when it heads a line (markdown heading or bold/list
    # markup stripped), so a casual prose mention doesn't count as present.
    heads = {re.sub(r"^[#>*_\-\s]+", "", ln).strip().lower() for ln in (body or "").splitlines()}
    return [s for s in sections if not any(h.startswith(s.lower()) for h in heads)]


def run(repo, pr_number):
    """Check the PR body for required sections, upsert the verdict. Host-agnostic core."""
    missing = missing_sections(repo.get_pull(pr_number).body, REQUIRED_SECTIONS)
    if missing:
        msg = (
            "### ❌ PR description check\n"
            "Missing required section(s): "
            + ", ".join(f"**{m}**" for m in missing)
            + "\n\nPlease add them to the PR description."
        )
    else:
        msg = (
            "### ✅ PR description check\n"
            "All required sections present: " + ", ".join(REQUIRED_SECTIONS) + "."
        )
    gh.upsert_comment(repo, pr_number, "bot:validate", msg)


def main():
    run(gh.get_repo(), int(os.environ["PR_NUMBER"]))


if __name__ == "__main__":
    main()
