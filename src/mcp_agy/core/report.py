"""The report contract: how a worker run hands its outcome back to the architect.

Every task prompt gets the same closing instruction, so the reply always ends in one small block
the architect can read without knowing how the prompt was phrased - and without the architect
having to remember to ask for it. The block is parsed into `WorkerReport`; when AGY does not write
one, the result simply carries `report=None` and the full response is still there.
"""

from __future__ import annotations

import re
from typing import Optional

from mcp_agy.core.models import WorkerReport

_BLOCK_SHAPE = (
    "REPORT\n"
    "STATUS: done | partial | blocked\n"
    "SUMMARY: <one or two sentences>\n"
    "CHANGED: <files you created or edited, comma-separated, or none>\n"
    "CHECKS: <commands you ran to check the work and their result, or none>\n"
    "BLOCKERS: <what is unresolved or needs a decision, or none>"
)

_EDIT_CONTRACT = (
    "\n\n---\n"
    "End your reply with this block, filled in, and nothing after it. Keep everything above it "
    "brief - the reviewer reads the diff, not the narration.\n" + _BLOCK_SHAPE
)

# For a read-only run the findings are the deliverable, so the contract must not read as "be brief".
_PLAN_CONTRACT = (
    "\n\n---\n"
    "Write your findings first; they are the deliverable. Then end your reply with this block, "
    "filled in, and nothing after it.\n" + _BLOCK_SHAPE
)

_HEADER = re.compile(r"^[\s>*#_`-]*REPORT[\s*_`:]*$", re.IGNORECASE)
_FIELD = re.compile(
    r"^[\s>*_`-]*(STATUS|SUMMARY|CHANGED|CHECKS|BLOCKERS)[\s*_`]*:\s*(.*?)\s*$", re.IGNORECASE
)
_NONE_VALUES = {"", "none", "n/a", "na", "-", "nothing", "no"}
_STATUSES = ("done", "partial", "blocked")


def with_report_contract(prompt: str, mode: str) -> str:
    """Append the closing REPORT instruction for a run in `mode`."""
    return prompt + (_PLAN_CONTRACT if mode == "plan" else _EDIT_CONTRACT)


def _clean(value: str) -> str:
    value = value.strip().strip("`*_").strip()
    return "" if value.lower() in _NONE_VALUES else value


def _status(value: str) -> str:
    # A reply that pastes the template line verbatim ("done | partial | blocked") has not chosen.
    if "|" in value:
        return "unknown"
    words = re.findall(r"[a-z]+", value.lower())
    return words[0] if words and words[0] in _STATUSES else "unknown"


def parse_report(response: str) -> Optional[WorkerReport]:
    """Lift the last REPORT block out of a reply. None when there is no block with a STATUS line."""
    if not response:
        return None

    lines = response.splitlines()
    start = next((i for i in range(len(lines) - 1, -1, -1) if _HEADER.match(lines[i])), None)
    if start is None:
        # No header line: anchor on the last STATUS line so a block written without one still counts.
        start = next(
            (
                i - 1
                for i in range(len(lines) - 1, -1, -1)
                if (m := _FIELD.match(lines[i])) and m.group(1).upper() == "STATUS"
            ),
            None,
        )
        if start is None:
            return None

    fields: dict[str, str] = {}
    for line in lines[start + 1 :]:
        match = _FIELD.match(line)
        if match:
            fields.setdefault(match.group(1).upper(), match.group(2))
    if "STATUS" not in fields:
        return None

    changed = _clean(fields.get("CHANGED", ""))
    return WorkerReport(
        status=_status(fields["STATUS"]),  # type: ignore[arg-type]
        summary=_clean(fields.get("SUMMARY", "")),
        changed=[f for f in (_clean(p) for p in re.split(r"[,;]", changed)) if f] if changed else [],
        checks=_clean(fields.get("CHECKS", "")),
        blockers=_clean(fields.get("BLOCKERS", "")),
    )
