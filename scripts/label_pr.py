"""Auto-label a PR from its changed file paths (no AI)."""

import fnmatch
import os

from scripts import gh
from scripts.config import LABEL_RULES


def labels_for(paths, rules):
    out = set()
    for path in paths:
        for pattern, label in rules.items():
            if fnmatch.fnmatch(path, pattern):
                out.add(label)
    return sorted(out)


def run(repo, pr_number):
    """Label the PR from its changed paths, reconciled so re-running stays consistent
    as the file mix changes (stale managed labels removed). Host-agnostic core."""
    paths = [f.filename for f in repo.get_pull(pr_number).get_files()]
    managed = sorted(set(LABEL_RULES.values()))
    gh.set_managed_labels(repo, pr_number, labels_for(paths, LABEL_RULES), managed)


def main():
    run(gh.get_repo(), int(os.environ["PR_NUMBER"]))


if __name__ == "__main__":
    main()
