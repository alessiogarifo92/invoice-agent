"""Store one run's rule results: a `flags` row per flag and the proposal's rule floor.

Runs in the caller's transaction (the worker commits it with the stage output), like
`append_event`. `flags` has no column for the message or the action floor, so the row's
`evidence` holds `{"message", "action_floor", "details"}`, `details` being the rule's own
evidence. The proposal starts with `final_action = rule_floor` and no agent action: the
agent, when it runs, can only raise it (`final = max(rule_floor, agent_action)`).

`extraction_flags` are the run's extraction flags (schema or grounding failure), already
written by `persist_extraction`: they count toward the floor and the rationale, so a value
the model invented can never be approved, but they are not inserted a second time.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from app.rules.flags import Action, Flag, rule_floor


@dataclass(frozen=True)
class PersistedRules:
    proposal_id: UUID
    flag_ids: tuple[UUID, ...]
    rule_floor: Action


def _raised(source: str, flags: Sequence[Flag]) -> str:
    codes = ", ".join(dict.fromkeys(f.code for f in flags))
    noun = "flag" if len(flags) == 1 else "flags"
    return f"{source} raised {len(flags)} {noun}: {codes}."


def _rationale(flags: Sequence[Flag], extraction_flags: Sequence[Flag] = ()) -> str:
    text = _raised("Rules", flags) if flags else "No rule raised a flag."
    if extraction_flags:
        text += " " + _raised("Extraction", extraction_flags)
    return text


def persist_rule_results(
    conn: Connection,
    invoice_id: UUID | str,
    run_id: UUID | str,
    flags: Sequence[Flag],
    extraction_flags: Sequence[Flag] = (),
) -> PersistedRules:
    flag_ids = tuple(
        conn.execute(
            text(
                "INSERT INTO flags (invoice_id, run_id, code, severity, evidence) "
                "VALUES (:invoice_id, :run_id, :code, :severity, CAST(:evidence AS jsonb)) "
                "RETURNING id"
            ),
            {
                "invoice_id": invoice_id,
                "run_id": run_id,
                "code": f.code,
                "severity": f.severity,
                "evidence": json.dumps(
                    {"message": f.message, "action_floor": f.action_floor, "details": f.evidence}
                ),
            },
        ).scalar_one()
        for f in flags
    )
    floor = rule_floor([*flags, *extraction_flags])
    proposal_id = conn.execute(
        text(
            "INSERT INTO proposals (invoice_id, run_id, rule_floor, final_action, rationale) "
            "VALUES (:invoice_id, :run_id, :floor, :floor, :rationale) RETURNING id"
        ),
        {
            "invoice_id": invoice_id,
            "run_id": run_id,
            "floor": floor,
            "rationale": _rationale(flags, extraction_flags),
        },
    ).scalar_one()
    return PersistedRules(proposal_id=proposal_id, flag_ids=flag_ids, rule_floor=floor)
