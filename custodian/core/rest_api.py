from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from custodian.db.connection import db_connection
from custodian.db.system import get_config_value, set_config_value
from custodian.services.box_bridge import call_project_tool


ALLOWED_ORIGIN = "https://finance95-web.vercel.app"
FINANCE_PROJECT = "finance95-web"
REST_API_KEY_CONFIG = "rest_api_key"
REST_API_KEY_ENV = "CUSTODIAN_REST_API_KEY"
REST_API_MOUNT_FLAG = "_custodian_rest_api_mounted"


@dataclass
class RestApiError(Exception):
    status_code: int
    payload: dict[str, Any]


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
    }


def _json_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers=_cors_headers())


def _response(status_code: int = 204) -> Response:
    return Response(status_code=status_code, headers=_cors_headers())


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return ""
    return auth_header[7:].strip()


def _tool_exists(project: str, tool_name: str) -> bool:
    with db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM tool_registry WHERE project = ? AND tool_name = ? LIMIT 1",
            (project, tool_name),
        ).fetchone()
    return row is not None


def _get_or_create_rest_api_key() -> tuple[str, str, bool]:
    env_token = os.environ.get(REST_API_KEY_ENV, "").strip()
    if env_token:
        return env_token, "env", False

    with db_connection() as conn:
        stored = get_config_value(conn, REST_API_KEY_CONFIG)
        if stored:
            return stored, "db", False
        token = secrets.token_hex(32)
        set_config_value(conn, REST_API_KEY_CONFIG, token)
        return token, "db", True


def _ensure_authorized(request: Request) -> None:
    expected = getattr(request.app.state, "rest_api_key", "")
    provided = _extract_bearer_token(request)
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise RestApiError(401, {"error": "Unauthorized"})


async def _parse_json_body(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RestApiError(400, {"error": "Invalid parameters", "detail": f"invalid JSON: {exc.msg}"}) from exc

    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise RestApiError(400, {"error": "Invalid parameters", "detail": "request body must be a JSON object"})
    return payload


def _stringify_error(detail: Any) -> str:
    if isinstance(detail, dict):
        if isinstance(detail.get("error"), str):
            return detail["error"]
        if isinstance(detail.get("detail"), str):
            return detail["detail"]
        return json.dumps(detail)
    return str(detail)


def _map_error(project: str, tool_name: str, response: dict[str, Any]) -> RestApiError:
    message = _stringify_error(response.get("detail") or response.get("error") or response)
    lowered = message.lower()

    if "tool not found" in lowered:
        return RestApiError(404, {"error": "Tool not found", "tool": tool_name})
    if any(fragment in lowered for fragment in ("container not running", "no tool server running", "bridge request failed", "connection refused")):
        return RestApiError(503, {"error": "Project box not available", "project": project})
    if any(fragment in lowered for fragment in ("invalid json", "must be", "required", "validation error")):
        return RestApiError(400, {"error": "Invalid parameters", "detail": message})
    return RestApiError(500, {"error": "Tool failed", "detail": message})


def _call_box_tool(project: str, tool_name: str, params: dict[str, Any] | None = None) -> Any:
    if not _tool_exists(project, tool_name):
        raise RestApiError(404, {"error": "Tool not found", "tool": tool_name})

    response = call_project_tool(project=project, tool_name=tool_name, params=params or {})
    if isinstance(response, dict):
        if "result" in response:
            return response["result"]
        raise _map_error(project, tool_name, response)
    return response


def _query_int(request: Request, key: str) -> int | None:
    raw = request.query_params.get(key)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RestApiError(400, {"error": "Invalid parameters", "detail": f"'{key}' must be an integer"}) from exc


def _query_value(request: Request, key: str) -> str | None:
    raw = request.query_params.get(key)
    return raw if raw not in (None, "") else None


def _json_result(result: Any) -> JSONResponse:
    return _json_response({"result": result})


async def _options(_request: Request) -> Response:
    return _response()


async def _handle_rest_api_error(_request: Request, exc: RestApiError) -> JSONResponse:
    return _json_response(exc.payload, status_code=exc.status_code)


async def _handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else _stringify_error(exc.detail)
    return _json_response({"error": "Tool failed", "detail": detail}, status_code=exc.status_code)


async def _handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("uvicorn.error").exception("[custodian] REST API route failed", exc_info=exc)
    return _json_response({"error": "Tool failed", "detail": str(exc)}, status_code=500)


async def _box_proxy(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    payload = await _parse_json_body(request)
    result = _call_box_tool(request.path_params["project"], request.path_params["tool_name"], payload)
    return _json_result(result)


async def _finance_list_goals(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "list_goals", {"status": "active"}))


async def _finance_create_goal(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "create_goal", await _parse_json_body(request)))


async def _finance_update_goal(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    payload = await _parse_json_body(request)
    payload["id"] = request.path_params["goal_id"]
    return _json_result(_call_box_tool(FINANCE_PROJECT, "update_goal", payload))


async def _finance_delete_goal(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "delete_goal", {"id": request.path_params["goal_id"]}))


async def _finance_accounts(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_bank_accounts", {}))


async def _finance_transactions(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    params = {
        key: value
        for key, value in {
            "days": _query_int(request, "days"),
            "search": _query_value(request, "search"),
            "account_id": _query_value(request, "account_id"),
        }.items()
        if value is not None
    }
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_bank_transactions", params))


async def _finance_spending(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    params = {}
    month = _query_value(request, "month")
    if month is not None:
        params["month"] = month
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_spending_summary", params))


async def _finance_categories(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "list_categories", {}))


async def _finance_categorize(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "categorize_transaction", await _parse_json_body(request)))


async def _finance_auto_categorize(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "auto_categorize", await _parse_json_body(request)))


async def _finance_brokerage_accounts(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_brokerage_accounts", {}))


async def _finance_positions(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    params = {}
    account_id = _query_value(request, "account_id")
    if account_id is not None:
        params["account_id"] = account_id
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_positions", params))


async def _finance_brokerage_summary(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_portfolio_summary", {}))


async def _finance_hours(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    params = {
        key: value
        for key, value in {
            "job": _query_value(request, "job"),
            "weeks": _query_int(request, "weeks"),
        }.items()
        if value is not None
    }
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_hours_log", params))


async def _finance_income_projection(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    params = {}
    months_ahead = _query_int(request, "months_ahead")
    if months_ahead is not None:
        params["months_ahead"] = months_ahead
    return _json_result(_call_box_tool(FINANCE_PROJECT, "get_income_projection", params))


async def _finance_sync(request: Request) -> JSONResponse:
    _ensure_authorized(request)
    if not _tool_exists(FINANCE_PROJECT, "simplefin_sync"):
        raise RestApiError(404, {"error": "Tool not found", "tool": "simplefin_sync"})
    return _json_result(_call_box_tool(FINANCE_PROJECT, "simplefin_sync", {}))


def _rest_routes() -> list[Route]:
    return [
        Route("/box/{project:str}/{tool_name:str}", endpoint=_box_proxy, methods=["POST"]),
        Route("/finance/goals", endpoint=_finance_list_goals, methods=["GET"]),
        Route("/finance/goals", endpoint=_finance_create_goal, methods=["POST"]),
        Route("/finance/goals/{goal_id:str}", endpoint=_finance_update_goal, methods=["PUT"]),
        Route("/finance/goals/{goal_id:str}", endpoint=_finance_delete_goal, methods=["DELETE"]),
        Route("/finance/accounts", endpoint=_finance_accounts, methods=["GET"]),
        Route("/finance/transactions", endpoint=_finance_transactions, methods=["GET"]),
        Route("/finance/spending", endpoint=_finance_spending, methods=["GET"]),
        Route("/finance/categories", endpoint=_finance_categories, methods=["GET"]),
        Route("/finance/categorize", endpoint=_finance_categorize, methods=["POST"]),
        Route("/finance/auto-categorize", endpoint=_finance_auto_categorize, methods=["POST"]),
        Route("/finance/brokerage/accounts", endpoint=_finance_brokerage_accounts, methods=["GET"]),
        Route("/finance/brokerage/positions", endpoint=_finance_positions, methods=["GET"]),
        Route("/finance/brokerage/summary", endpoint=_finance_brokerage_summary, methods=["GET"]),
        Route("/finance/hours", endpoint=_finance_hours, methods=["GET"]),
        Route("/finance/income-projection", endpoint=_finance_income_projection, methods=["GET"]),
        Route("/finance/sync", endpoint=_finance_sync, methods=["POST"]),
        Route("/{path:path}", endpoint=_options, methods=["OPTIONS"]),
    ]


async def setup_rest_routes(app: Starlette) -> None:
    if getattr(app.state, REST_API_MOUNT_FLAG, False):
        return

    token, source, generated = _get_or_create_rest_api_key()
    rest_app = Starlette(
        routes=_rest_routes(),
        exception_handlers={
            RestApiError: _handle_rest_api_error,
            StarletteHTTPException: _handle_http_exception,
            Exception: _handle_unexpected_error,
        },
    )
    rest_app.state.rest_api_key = token
    app.router.routes.append(Mount("/api", app=rest_app))
    setattr(app.state, REST_API_MOUNT_FLAG, True)

    logger = logging.getLogger("uvicorn.error")
    if generated:
        logger.info("[custodian] REST API key generated and stored in DB: %s", token)
    else:
        logger.info("[custodian] REST API enabled at /api using key from %s", source)
