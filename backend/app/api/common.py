"""What every router shares: the error body, and the settings the app was built with."""

from typing import cast

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings


class ErrorResponse(BaseModel):
    code: str
    message: str


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(code=code, message=message).model_dump(),
        headers=headers,
    )
