"""Delivery of the AI Deep Reasoning Toolkit into the workspace AGY runs in.

    https://github.com/DuongNAD/ai-deep-reasoning-toolkit

The toolkit is a rule file plus a skill that constrain how Gemini/Antigravity writes code.
Antigravity reads `GEMINI.md` and `.agents/skills/` from the directory it is working in, and
this server already spawns `agy` with the workspace as its cwd - so putting those files in the
workspace is the whole mechanism. Claude Code, Cursor and Copilot read different filenames, so
installing here changes the worker's behaviour and leaves the architect's untouched. That
isolation is the toolkit's stated design, and it is why `AGENTS.md` is never written: both
agents read that name.

Two things this module exists to do, in order of how much they are worth:

1. **Name the skill.** The toolkit's own benchmark measured `deep-verify` self-activating in
   0 of 10 runs on a task built to exactly its stated shape, and 5 of 5 once the prompt named
   the trade-offs. Discovery is what fails, not the skill. The architect is the only party that
   knows whether a task has two real designs, so `rigor="deep"` lets it say so out loud.
   The same benchmark is why `deep` is not the default: on a fully specified contract it scored
   identically and shipped 37% more code.

2. **Report whether the protocol actually ran.** `GEMINI.md` section 4.4 mandates a line
   beginning `Simplicity gate:` on every response that ships code, and the skill mandates a
   one-line declination when it decides not to activate. Both are fixed formats, so both can be
   lifted out of the prose and handed back as fields. That is the same signal the benchmark
   leans on to conclude the rule file was loaded at all.

Nothing here is on by default: with `MCP_AGY_TOOLKIT_PATH` unset, every call in this module is
a no-op and the server behaves exactly as it did before.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Optional, Tuple

from mcp_agy.utils.logger import get_logger

logger = get_logger("mcp_agy.core.reasoning_toolkit")

TOOLKIT_ENV = "MCP_AGY_TOOLKIT_PATH"
TOOLKIT_URL = "https://github.com/DuongNAD/ai-deep-reasoning-toolkit"

# The two artefacts the toolkit's own installer places, and the only two written here.
RULES_FILE = "GEMINI.md"
SKILL_DIR = Path(".agents") / "skills" / "deep-verify"

# Mandated by GEMINI.md 4.4: "Every response that ships code MUST end with the gate line."
_GATE_LINE = re.compile(r"^[ \t>*_-]*(Simplicity gate:.*)$", re.MULTILINE | re.IGNORECASE)

# Mandated by the skill: "When you decline, state it in one line."
_DEEP_VERIFY_DECLINED = re.compile(r"^[ \t>*_-]*deep-verify:\s*not activated\b", re.MULTILINE | re.IGNORECASE)

# One `git rev-parse` per server process. The checkout does not move under us mid-run, and the
# revision is provenance on every single result - re-shelling for it per call buys nothing.
_revision_cache: dict[str, str] = {}


def toolkit_root() -> Optional[Path]:
    """The configured toolkit checkout, or None when the integration is switched off.

    A path that is set but does not contain `GEMINI.md` is treated as not configured, and says
    so in the log: pointing at the wrong directory should not look like a working install.
    """
    raw = (os.environ.get(TOOLKIT_ENV) or "").strip()
    if not raw:
        return None

    root = Path(raw).expanduser()
    if not (root / RULES_FILE).is_file():
        logger.warning(
            f"{TOOLKIT_ENV}={raw!r} does not contain {RULES_FILE}; reasoning toolkit disabled. "
            f"Clone it from {TOOLKIT_URL} and point {TOOLKIT_ENV} at the checkout."
        )
        return None
    return root


def toolkit_revision(root: Path) -> str:
    """Short git revision of the toolkit checkout, or "" when it is not a repo.

    Provenance, not decoration: two runs of the same task under two toolkit revisions are two
    different experiments, and a result that cannot name its arm cannot be compared with another.
    """
    key = str(root)
    if key in _revision_cache:
        return _revision_cache[key]

    revision = ""
    try:
        proc = subprocess.run(
            [shutil.which("git") or "git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        if proc.returncode == 0:
            revision = proc.stdout.strip()
    except Exception as exc:  # not a repo, git missing, timeout
        logger.debug(f"Could not read toolkit revision at '{root}': {exc}")

    _revision_cache[key] = revision
    return revision


def _hide_from_git(workspace: Path, entries: list[str]) -> None:
    """Register installed paths in `.git/info/exclude` so they stay out of the user's repo.

    This is the whole reason `agy_get_diff` and `modified_files` do not need a filter of their
    own: both read `git status`, and git will not report a path it has been told to ignore. The
    alternative - a hardcoded skip list in the diff engine - would have to be kept in sync with
    this module forever, and would still leave the files showing up in the user's own
    `git status`.

    `.git/info/exclude` is the right file rather than `.gitignore`: it is local to the clone and
    is never committed, so nothing here ends up in the user's history. A workspace that is not a
    git repo simply has nowhere to write, which is not an error.
    """
    info_dir = workspace / ".git" / "info"
    if not (workspace / ".git").is_dir():
        return

    exclude = info_dir / "exclude"
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        missing = [e for e in entries if e not in existing.splitlines()]
        if not missing:
            return

        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}# added by mcp-agy: AI Deep Reasoning Toolkit, not part of this project\n")
            fh.write("\n".join(missing) + "\n")
        logger.info(f"Excluded toolkit paths from git in '{exclude}': {', '.join(missing)}")
    except OSError as exc:
        # Worth saying out loud: without the exclusion the files show up as untracked in every
        # diff this server reports, which is noise the architect pays for in context.
        logger.warning(f"Could not update '{exclude}': {exc}. Toolkit files will show as untracked.")


def provision(workspace: Path, root: Path) -> Tuple[list[str], str]:
    """Place the toolkit in `workspace`, never overwriting. Returns (installed_paths, notes).

    An existing `GEMINI.md` is left exactly as it is. It may be the user's own rules for this
    repo, and there is no meaningful way to merge two rule files - silently replacing one would
    change how every future run in that workspace behaves, which is not a decision this server
    gets to make on its own.

    Racing callers are harmless: `mode="plan"` takes a shared lock, so two investigations of one
    repo can provision at once, and both write byte-identical content.
    """
    installed: list[str] = []
    notes: list[str] = []

    rules_dst = workspace / RULES_FILE
    if rules_dst.exists():
        notes.append(f"{RULES_FILE} already present, left unchanged")
    else:
        shutil.copy2(root / RULES_FILE, rules_dst)
        installed.append(RULES_FILE)

    skill_src = root / SKILL_DIR
    skill_dst = workspace / SKILL_DIR
    if skill_dst.exists():
        notes.append(f"{SKILL_DIR.as_posix()} already present, left unchanged")
    elif skill_src.is_dir():
        skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_src, skill_dst, dirs_exist_ok=True)
        installed.append(SKILL_DIR.as_posix())
    else:
        notes.append(f"{SKILL_DIR.as_posix()} missing from the toolkit checkout")

    # Register both paths regardless of who wrote them: a file installed by an earlier run, or
    # by the toolkit's own install.sh, still pollutes this server's diffs if git can see it.
    _hide_from_git(workspace, [f"/{RULES_FILE}", f"/{SKILL_DIR.as_posix()}/"])

    return installed, "; ".join(notes)


def deep_verify_preamble() -> str:
    """The sentence that makes `deep-verify` discoverable, prepended to the caller's prompt.

    Kept to what the skill itself asks for. The closing instruction is not politeness: the skill
    states that declining is a valid and common outcome, and that a matrix of one real design
    against two strawmen "launders a foregone conclusion as deliberation". An architect that
    demands the pipeline unconditionally gets exactly that.
    """
    return (
        "Before you start: treat this as a task with more than one genuinely different viable "
        "design, where choosing wrong is expensive to reverse. Use the `deep-verify` skill in "
        ".agents/skills/deep-verify by name, and produce its mandatory Comparative Matrix.\n"
        "If Step 1 shows the contract admits only one reasonable design, do not manufacture "
        "alternatives to compare - decline in the single line the skill specifies, and solve it "
        "directly under the GEMINI.md stages instead.\n\n"
    )


def read_protocol_report(response: str) -> Tuple[str, bool]:
    """Lift the toolkit's two mandated one-liners out of a response.

    Returns (gate_line, deep_verify_declined).

    Only these two are parsed, because only these two have a format the toolkit actually
    mandates. The Stage 0 tier declaration is required to be one line but its wording is left
    open, and "activated" has no declared marker at all - guessing at either would hand the
    architect a field that is confidently wrong some of the time, which is worse than no field.
    """
    if not response:
        return "", False

    match = _GATE_LINE.search(response)
    return (match.group(1).strip() if match else ""), bool(_DEEP_VERIFY_DECLINED.search(response))
