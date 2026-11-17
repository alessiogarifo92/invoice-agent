"""Store one run's rule results: a `flags` row per flag and the proposal's rule floor.

Runs in the caller's transaction (the worker commits it with the stage output), like
`append_event`. `flags` has no column for the message or the action floor, so the row's
`evidence` holds `{"message", "action_floor", "details"}`, `details` being the rule's own
evidence. The proposal starts with `final_action = rule_floor` and no agent action: the
agent, when it runs, can only raise it (`final = max(rule_floor, agent_action)`).
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


def _rationale(flags: Sequence[Flag]) -> str:
    if not flags:
        return "No rule raised a flag."
    codes = ", ".join(dict.fromkeys(f.code for f in flags))
    noun = "flag" if len(flags) == 1 else "flags"
    return f"Rules raised {len(flags)} {noun}: {codes}."


def persist_rule_results(
    conn: Connection, invoice_id: UUID | str, run_id: UUID | str, flags: Sequence[Flag]
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
    floor = rule_floor(flags)
    proposal_id = conn.execute(
        text(
            "INSERT INTO proposals (invoice_id, run_id, rule_floor, final_action, rationale) "
            "VALUES (:invoice_id, :run_id, :floor, :floor, :rationale) RETURNING id"
        ),
        {
            "invoice_id": invoice_id,
            "run_id": run_id,
            "floor": floor,
            "rationale": _rationale(flags),
        },
    ).scalar_one()
    return PersistedRules(proposal_id=proposal_id, flag_ids=flag_ids, rule_floor=floor)
