#!/usr/bin/env python3
"""Fail when one action's subpaths are pinned to different commits.

THE FAILURE THIS EXISTS TO CATCH IS MEASURED, not hypothetical. Dependabot PR #12
bumped `github/codeql-action/init` 4.37.9 -> 4.38.0 and nothing else, because
Dependabot treats `init`, `analyze` and `upload-sarif` as three separate
dependencies. Both CodeQL matrix jobs then died with:

    Loaded a configuration file for version '4.38.0', but running version '4.37.9'

Nothing in CI caught that. CodeRabbit did, in prose, on one PR. `.github/dependabot.yml`
now groups the codeql-action subpaths so Dependabot cannot raise that PR again — but a
group in a config file is a policy, and a hand edit to a workflow is not bound by it.
This is the gate that binds it.

THE RULE IS GENERAL, deliberately. It groups by `owner/repo` — the first two path
segments — so `github/codeql-action/init` and `github/codeql-action/analyze` fall in one
group while `actions/checkout` and `actions/setup-python` stay separate, as they should.
Any future multi-subpath action is covered without an edit here.

AND IT RUNS TWO-SIDED, for the reason tests/check_gitleaks_gate.py spells out at length:
"no divergence found" is what a working checker reports on a consistent tree and what a
checker with a broken regex reports on ANY tree. Those are indistinguishable from the
green tick. So every run also plants a divergence into a throwaway copy and requires the
checker to find it. The previous build's retrospective counted five gates in this fleet
that could not fail; this one proves it can, on every single run, rather than once by
hand at review time.

Run locally with:  python tests/check_action_pins.py
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"

# `uses: owner/repo[/subpath]@<40-hex>` with an optional trailing version comment.
#
# The 40-hex is required rather than optional, and that is a scope decision worth
# stating: a tag-pinned `uses:` is a different defect (B12/F7) with its own check in
# repo-conform, and matching it here would make THIS gate red for a reason its name
# does not describe. A `uses: ./.github/workflows/x.yml` local reference has no `@` at
# all and is correctly ignored.
# Horizontal whitespace only — `\s` matches newlines, so `^\s*` would happily start the
# match on a blank line ABOVE the `uses:` and report a line number two off. A gate that
# names the wrong line sends the reader to the wrong place, which is its own small lie.
#
# QUOTED SCALARS ARE MATCHED TOO. `uses: "owner/repo/sub@<sha>"` is ordinary YAML and
# Actions reads it identically to the bare form. Nothing in this repo writes it that way
# today, which is exactly why it is worth handling: a pin this pattern does not see is a
# pin that can diverge without the gate noticing, and "no workflow uses that syntax" is a
# fact about the present tree, not a property of the check. `(?(q)(?P=q))` is a
# conditional backreference — it demands the SAME closing quote when an opening one was
# matched, and demands nothing when it was not, so an unbalanced quote is not accepted as
# a pin.
USES = re.compile(
    r"^[ \t]*(?:-[ \t]+)?uses:[ \t]*(?P<q>[\'\"])?"
    r"(?P<ref>[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+)@(?P<sha>[0-9a-f]{40})"
    r"(?(q)(?P=q))"
    r"(?:[ \t]*#[ \t]*(?P<comment>.*?))?[ \t]*$",
    re.MULTILINE,
)

# A SHA that is not any real commit, used only for the planted side.
PLANTED_SHA = "0123456789abcdef0123456789abcdef01234567"


class Pin:
    __slots__ = ("comment", "file", "line", "ref", "sha")

    def __init__(self, ref: str, sha: str, comment: str | None, file: Path, line: int):
        self.ref = ref
        self.sha = sha
        self.comment = (comment or "").strip()
        self.file = file
        self.line = line

    @property
    def group(self) -> str:
        """`owner/repo` — the unit that must move as one."""
        return "/".join(self.ref.split("/")[:2])

    def __str__(self) -> str:
        tail = f"  # {self.comment}" if self.comment else ""
        return f"{self.file.name}:{self.line}  {self.ref}@{self.sha[:12]}…{tail}"


def collect_pins(workflows: Path) -> list[Pin]:
    pins: list[Pin] = []
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        text = path.read_text()
        for m in USES.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            pins.append(Pin(m["ref"], m["sha"], m["comment"], path, line))
    return pins


def find_divergence(workflows: Path) -> dict[str, list[Pin]]:
    """Groups pinned to more than one distinct commit. Empty dict means consistent."""
    groups: dict[str, list[Pin]] = defaultdict(list)
    for pin in collect_pins(workflows):
        groups[pin.group].append(pin)
    return {g: pins for g, pins in groups.items() if len({p.sha for p in pins}) > 1}


def _report(divergent: dict[str, list[Pin]]) -> None:
    for group, pins in sorted(divergent.items()):
        print(f"      {group} is pinned to {len({p.sha for p in pins})} different commits:")
        for pin in pins:
            print(f"        {pin}")


def _plant(group: str, target: Pin, quote: str) -> bool:
    """Rewrite one pin in a throwaway copy and require find_divergence to report it.

    `quote` is "" for the bare form and `'` or `"` for a YAML quoted scalar. Both are
    exercised on every run: the quoted form is not used anywhere in this repo today, so
    the only thing that would ever catch the pattern losing it is a probe that writes it
    deliberately.
    """
    form = f"quoted with {quote}" if quote else "bare"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "workflows"
        shutil.copytree(WORKFLOWS, work)

        planted_file = work / target.file.name
        before = planted_file.read_text()
        after = before.replace(
            f"{target.ref}@{target.sha}",
            f"{quote}{target.ref}@{PLANTED_SHA}{quote}",
            1,
        )
        if after == before:
            print(
                f"FAIL  could not plant a {form} divergence into {target.file.name} — "
                "the substitution did not match, so this side never ran. Do not read "
                "the clean side as a pass."
            )
            return False
        planted_file.write_text(after)

        divergent = find_divergence(work)
        if group not in divergent:
            print(
                f"FAIL  planted a second commit for {group} in {target.file.name} "
                f"({form}) and the checker did NOT report it. The gate cannot fail, so "
                "its green result on the real tree means nothing."
            )
            return False

    print(f"ok    planted divergence in {group} was detected ({form})")
    return True


def check_planted_fails() -> bool:
    """Plant a divergence in a copy and require the checker to find it."""
    groups: dict[str, list[Pin]] = defaultdict(list)
    for pin in collect_pins(WORKFLOWS):
        groups[pin.group].append(pin)

    # THE PLANT MUST BE ABLE TO LAND. A group with one member cannot diverge, so
    # mutating it proves nothing — the planted side would pass for the wrong reason and
    # report a checker that never fired as a working gate.
    multi = {g: ps for g, ps in groups.items() if len(ps) > 1}
    if not multi:
        print(
            "FAIL  no action is used from more than one subpath/file, so a divergence "
            "cannot be planted and this gate cannot be shown to fail. Its green result "
            "on the real tree would carry no information."
        )
        return False

    group, victims = max(multi.items(), key=lambda kv: len(kv[1]))
    target = victims[1]  # the second occurrence; the first keeps the true SHA

    # Every accepted spelling gets planted, not just the one the tree happens to use.
    return all(_plant(group, target, q) for q in ("", '"', "'"))


def check_real_tree_passes() -> bool:
    if not WORKFLOWS.is_dir():
        print(f"FAIL  {WORKFLOWS} does not exist — nothing was scanned.")
        return False

    pins = collect_pins(WORKFLOWS)
    if not pins:
        print(
            f"FAIL  no SHA-pinned `uses:` found under {WORKFLOWS}. Either the workflows "
            "are unpinned or the pattern stopped matching; both make a clean result a "
            "lie rather than a pass."
        )
        return False

    divergent = find_divergence(WORKFLOWS)
    if divergent:
        print("FAIL  the real tree has divergent action pins:")
        _report(divergent)
        print(
            "      Every subpath of one action must move to the same commit together. "
            "Bump them in one edit."
        )
        return False

    groups = sorted({p.group for p in pins})
    print(f"ok    {len(pins)} pins across {len(groups)} actions, one commit each")
    return True


def main() -> int:
    # Planted first, and RETURN before the clean check rather than collecting both
    # results. Evaluating both would print `ok  N pins across M actions` even when the
    # planted side had just reported that the gate cannot fail — which is the precise
    # false reassurance this file exists to refuse. The comment said "should not be
    # printed as though it were" while the code printed it anyway; CodeRabbit caught
    # that on PR #13.
    if not check_planted_fails():
        return 1
    if not check_real_tree_passes():
        return 1
    print("action-pin gate verified two-sided: it fails on a planted divergence and passes here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
