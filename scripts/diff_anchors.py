"""Parse a unified diff into the set of line numbers that can carry an inline
review comment.

GitHub anchors inline comments to the RIGHT (new-file) side: added (`+`) and
context (` `) lines within a hunk. Removed (`-`) lines are LEFT-only and can't be
anchored here. The GitHub Review API rejects the whole review if any comment
points at a line not in the diff, so callers must validate against this.
"""

import re
from fnmatch import fnmatch

from scripts.config import NOISE_GLOBS

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _walk(diff: str):
    """Yield (raw_line, path, new_line) per diff line; path/new_line are set only
    on RIGHT-side (context/added) lines, None otherwise. Single source of truth
    for the hunk-walking state machine — anchors() and number_diff() both ride it."""
    path: str | None = None
    new_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            # `+++ /dev/null` is a deletion — no RIGHT side to comment on.
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            yield raw, None, None
            continue
        m = _HUNK.match(raw)
        if m:
            new_line = int(m.group(1))
            yield raw, None, None
            continue
        if path is not None and raw and raw[0] in " +":
            yield raw, path, new_line
            new_line += 1
        else:
            # '-' removed line: LEFT-only, no anchor, don't advance new_line.
            # '\' (No newline at end of file) and headers: ignore.
            yield raw, None, None


def anchors(diff: str) -> dict[str, set[int]]:
    """Map each file path to its valid RIGHT-side comment line numbers."""
    out: dict[str, set[int]] = {}
    for _, path, n in _walk(diff):
        if path is not None:
            out.setdefault(path, set()).add(n)
    return out


def number_diff(diff: str) -> str:
    """Prefix each RIGHT-side line with its new-file number (`42| +code`) so the
    model can copy anchors instead of counting hunk offsets — LLMs are bad at
    counting, and a miscounted line silently demotes the issue to the summary."""
    return "\n".join(
        raw if n is None else f"{n}| {raw}" for raw, _, n in _walk(diff)
    )


def file_blocks(diff: str) -> list[str]:
    """Split a unified diff into per-file blocks on `diff --git` headers. A diff
    without those headers (e.g. a bare hunk) comes back as one block."""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and cur:
            blocks.append(cur)
            cur = []
        cur.append(line)
    if cur:
        blocks.append(cur)
    return ["".join(b) for b in blocks]


def block_path(block: str) -> str | None:
    """The file path a block touches: the new (`+++ b/`) path, falling back to the
    old (`--- a/`) path for deletions."""
    new = old = None
    for line in block.splitlines():
        t = line[4:].strip()
        if line.startswith("+++ ") and t != "/dev/null":
            new = t[2:] if t.startswith("b/") else t
        elif line.startswith("--- ") and t != "/dev/null":
            old = t[2:] if t.startswith("a/") else t
        if new:
            break
    return new or old


def strip_noise(diff: str, globs=NOISE_GLOBS) -> str:
    """Drop file blocks for generated/vendored paths (lock files, minified
    assets, …) so the token budget is spent on reviewable code."""
    kept = [
        b
        for b in file_blocks(diff)
        if not any(fnmatch(block_path(b) or "", g) for g in globs)
    ]
    return "".join(kept)


def _demo():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import os\n"          # context  -> line 1
        "-old = 1\n"            # removed  -> no anchor, no advance
        "+new = 2\n"            # added    -> line 2
        "+extra = 3\n"          # added    -> line 3
        " print(new)\n"        # context  -> line 4
        "diff --git a/del.txt b/del.txt\n"
        "--- a/del.txt\n"
        "+++ /dev/null\n"       # deletion -> no anchors
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    )
    assert anchors(diff) == {"foo.py": {1, 2, 3, 4}}, anchors(diff)
    print("ok")


if __name__ == "__main__":
    _demo()
