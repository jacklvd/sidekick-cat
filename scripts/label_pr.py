"""Auto-label a PR from its changed paths and the kind of change it makes (no AI).

Two independent axes, both deterministic:
  * WHAT it touches — path globs (LABEL_RULES): python, documentation, github-actions.
  * WHAT KIND of change — enhancement, bug, refactor, tests, chore (KIND_LABELS), guessed
    from the strongest signal available: the conventional-commit type in the PR title,
    else the branch name, else the diff's shape (only when unambiguous). When none of the
    three speaks — the usual outside-collaborator PR — the PR gets `needs-triage`.

Reads PR_NUMBER (Actions path); Cloud Run calls run() directly.
"""

import fnmatch
import os
import re

from scripts import gh
from scripts.config import KIND_LABELS, LABEL_RULES, NOISE_GLOBS, TRIAGE_LABEL

# `feat: x`, `feat(api): x`, `feat(api)!: x`, `FIX: x` — the conventional-commit type,
# with an optional scope and the breaking-change bang.
_TYPE_RE = re.compile(r"^\s*([a-zA-Z]+)\s*(?:\([^)]*\))?\s*!?\s*:")


def labels_for(paths, rules):
    out = set()
    for path in paths:
        for pattern, label in rules.items():
            if fnmatch.fnmatch(path, pattern):
                out.add(label)
    return sorted(out)


def kind_from_title(title: str) -> "str | None":
    """The change-kind label from the PR title's conventional-commit type, or None.

    The title beats the diff because it is the author stating intent outright, rather
    than us inferring it from line counts."""
    match = _TYPE_RE.match(title or "")
    return KIND_LABELS.get(match.group(1).lower()) if match else None


def kind_from_branch(ref: str) -> "str | None":
    """The change-kind label guessed from the branch name, or None.

    `fix/leaky-filter`, `feat-dark-mode`, `jackie/chore/bump-deps` — the first word
    anywhere in the branch that names a conventional-commit type wins. Weaker evidence
    than the title (a branch is named before the work is finished, and a stray word like
    `test-harness` can misfire), so it only speaks once the title has said nothing.

    dev-note: whole-word scan, no position rules. Tighten to leading segments only if
    branches here start misfiring.
    """
    for word in re.split(r"[^a-zA-Z]+", ref or ""):
        label = KIND_LABELS.get(word.lower())
        if label:
            return label
    return None


def kind_from_diff(files, globs=NOISE_GLOBS) -> "str | None":
    """Fallback for a title with no conventional-commit type. `files` is
    (path, status, additions, deletions) per changed file.

    Returns a label ONLY when the shape says something clear, and None otherwise. A diff
    cannot tell a bugfix from a feature — both edit existing files, and both usually add
    lines — so an ambiguous shape gets no kind label at all. A missing label is a small
    annoyance; a confidently wrong one is worse, because someone has to notice it to fix it.

    Generated/vendored files are dropped first: a lock-file bump is thousands of lines of
    churn that says nothing about intent.
    """
    real = [f for f in files if not any(fnmatch.fnmatch(f[0], g) for g in globs)]
    if not real:
        return None
    added = sum(a for _, _, a, _ in real)
    deleted = sum(d for _, _, _, d in real)
    statuses = {status for _, status, _, _ in real}
    # Whole files deleted, or the change is overwhelmingly removal → code is coming out.
    if "removed" in statuses or deleted > 2 * added:
        return "refactor"
    # Brand-new files, and little taken away → new surface area.
    if "added" in statuses and added > deleted:
        return "enhancement"
    return None  # edits to existing files could be anything, so say nothing


def desired_labels(paths, title, files, branch="") -> list:
    """Every label the bot wants here: what the PR touches, plus what kind of change it is.

    Kind is guessed strongest-signal-first — the title, then the branch, then the diff's
    shape — and when all three stay silent the PR gets TRIAGE_LABEL rather than nothing.
    An unlabeled PR looks like one the bot skipped; `needs-triage` says a human has to
    decide, which is the honest answer for `Update the scraper filter` on `patch-1`.
    """
    out = set(labels_for(paths, LABEL_RULES))
    kind = kind_from_title(title) or kind_from_branch(branch) or kind_from_diff(files)
    out.add(kind or TRIAGE_LABEL)
    return sorted(out)


def managed_labels() -> list:
    """The full universe the bot owns. Labels outside it (human-added) are never touched;
    ones inside it that are no longer wanted get removed on re-run — so retitling a
    guest's PR to `fix:` swaps needs-triage for bug on the next run."""
    return sorted(set(LABEL_RULES.values()) | set(KIND_LABELS.values()) | {TRIAGE_LABEL})


def run(repo, pr_number):
    """Label the PR, reconciled so a re-run stays consistent as the PR changes — retitling
    `feat:` to `fix:` swaps the label instead of accumulating both. Host-agnostic core."""
    pr = repo.get_pull(pr_number)
    files = [(f.filename, f.status, f.additions, f.deletions) for f in pr.get_files()]
    paths = [path for path, _, _, _ in files]
    gh.set_managed_labels(
        repo,
        pr_number,
        desired_labels(paths, pr.title, files, pr.head.ref),
        managed_labels(),
    )


def main():
    run(gh.get_repo(), int(os.environ["PR_NUMBER"]))


if __name__ == "__main__":
    main()
