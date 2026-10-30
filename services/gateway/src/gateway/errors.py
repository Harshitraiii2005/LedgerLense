"""One error shape for every failure: {"code", "message", "request_id"}.

Clients (and the Angular app) only ever need to handle this one schema, whether the
error came from validation, auth, a missing record, an upstream service or a bug.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ledgerlens_common.logging import request_id_var

log = logging.getLogger("gateway.errors")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body = {"code": code, "message": message, "request_id": request_id_var.get()}
    return JSONResponse(body, status_code=status)


# Map plain HTTP statuses (e.g. Starlette's own 404/405) to stable machine-readable codes.
_HTTP_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, e: ApiError) -> JSONResponse:
        return error_response(e.status, e.code, e.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return error_response(422, "validation_error", f"{loc}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, e: StarletteHTTPException) -> JSONResponse:
        return error_response(e.status_code, _HTTP_CODES.get(e.status_code, "http_error"), str(e.detail))

    @app.exception_handler(Exception)
    async def unhandled(_: Request, e: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return error_response(500, "internal_error", "internal server error")  # never leak internals
