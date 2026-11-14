from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection

from app.audit import verify_chain
from app.db import get_connection

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditVerifyResponse(BaseModel):
    ok: bool
    checked: int
    first_broken_seq: int | None
    reason: str | None
    head_hash: str | None


@router.get("/verify")
def verify(conn: Annotated[Connection, Depends(get_connection)]) -> AuditVerifyResponse:
    """Walk the audit hash chain. head_hash is the value to publish externally."""
    result = verify_chain(conn)
    return AuditVerifyResponse(
        ok=result.ok,
        checked=result.checked,
        first_broken_seq=result.first_broken_seq,
        reason=result.reason,
        head_hash=result.head_hash,
    )
