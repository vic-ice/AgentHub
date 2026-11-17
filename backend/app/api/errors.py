"""Centralized API error handling.

Combines types, classifiers, handlers, and SSE formatting previously
split across five files in the ``api/errors/`` package.

Architecture
------------
- **Types** — Custom exception hierarchy for LLM errors
- **Classifiers** — Pure functions for LLM error detection / classification
- **Handlers** — FastAPI exception handlers (HTTP / LLM / uncaught)
- **SSE** — Error payload formatting for SSE streaming responses
- **Registration** — Convenience function to wire handlers into a FastAPI app
"""

import logging
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.infra.errors import (
    AgentHubError,
    AgentTimeoutError,
    LLMError as DomainLLMError,
    ToolError as DomainToolError,
)
from app.utils.logging import get_request_id

logger = logging.getLogger(__name__)

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def resolve_request_id(candidate: str | None = None) -> str:
    """Return a log-safe correlation id, preserving valid caller ids."""
    value = (candidate or "").strip()
    if value and _SAFE_REQUEST_ID.fullmatch(value):
        return value
    return f"http-{uuid4().hex}"


def build_error_payload(
    *,
    detail: Any,
    error_type: str,
    error_code: str,
    stage: str,
    retryable: bool = False,
) -> dict[str, Any]:
    """Build the single diagnostic error contract used by HTTP and SSE."""
    request_id = get_request_id()
    return {
        "detail": detail,
        "error_type": error_type,
        "error_code": error_code,
        "request_id": request_id if request_id != "-" else "untracked",
        "stage": stage,
        "retryable": retryable,
    }


# =============================================================================
# Exception Types
# =============================================================================


class LLMBaseError(Exception):
    """Base exception for LLM-related errors."""

    pass


class LLMAuthenticationError(LLMBaseError):
    """Raised when LLM API key is invalid or authentication fails."""

    pass


class LLMConnectionError(LLMBaseError):
    """Raised when there is a network/connection issue with the LLM provider."""

    pass


class LLMInvalidRequestError(LLMBaseError):
    """Raised when the request to the LLM is invalid (bad parameters, etc.)."""

    pass


class LLMRateLimitError(LLMBaseError):
    """Raised when the LLM API rate limit is exceeded."""

    pass


class LLMPermissionError(LLMBaseError):
    """Raised when LLM API permission is denied (e.g. model not accessible)."""

    pass


class LLMUnknownProviderError(LLMBaseError):
    """Raised when the model provider is unknown or not supported."""

    pass


# =============================================================================
# Classifiers — LLM error detection & classification
# =============================================================================


def is_llm_authentication_error(exception: Exception) -> bool:
    """Detect if an exception is related to LLM authentication / API key issues."""
    error_str = str(exception).lower()
    class_name = type(exception).__name__.lower()

    auth_keywords = [
        "api key",
        "api_key",
        "authentication",
        "unauthorized",
        "401",
        "forbidden",
        "403",
        "invalid api key",
        "incorrect api key",
        "wrong key",
        "missing key",
    ]
    auth_type_names = ["authenticationerror", "autherror", "unauthorizederror"]

    if any(keyword in class_name for keyword in auth_type_names):
        return True
    if any(keyword in error_str for keyword in auth_keywords):
        return True
    return False


def is_llm_connection_error(exception: Exception) -> bool:
    """Detect if an exception is related to LLM connection / network issues."""
    error_str = str(exception).lower()
    class_name = type(exception).__name__.lower()

    connection_keywords = [
        "connection",
        "timeout",
        "network",
        "socket",
        "dns resolution",
        "dns error",
        "name resolution",
        "could not connect",
        "failed to connect",
        "connection refused",
        "connection reset",
        "econnrefused",
        "etimedout",
        "502",
        "503",
        "504",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
    ]
    connection_type_names = [
        "apiconnectionerror",
        "connectionerror",
        "timeouterror",
        "networkerror",
    ]

    if any(keyword in class_name for keyword in connection_type_names):
        return True
    if any(keyword in error_str for keyword in connection_keywords):
        return True
    return False


def is_llm_rate_limit_error(exception: Exception) -> bool:
    """Detect if an exception is related to LLM rate limiting."""
    error_str = str(exception).lower()
    class_name = type(exception).__name__.lower()

    rate_limit_keywords = [
        "rate limit",
        "ratelimit",
        "quota",
        "too many requests",
        "429",
        "rate exceeded",
        "request limit",
    ]
    rate_limit_type_names = [
        "ratelimiterror",
        "toolmanyrequests",
        "quotaexceedederror",
    ]

    if any(keyword in class_name for keyword in rate_limit_type_names):
        return True
    if any(keyword in error_str for keyword in rate_limit_keywords):
        return True
    return False


def is_llm_invalid_request_error(exception: Exception) -> bool:
    """Detect if an exception is related to invalid LLM request parameters."""
    error_str = str(exception).lower()
    class_name = type(exception).__name__.lower()

    invalid_request_keywords = [
        "invalid request",
        "bad request",
        "400",
        "invalid parameter",
        "missing parameter",
        "max tokens",
        "context length",
        "contextwindow",
        "maximum context",
    ]
    invalid_request_type_names = [
        "invalidrequesterror",
        "badrequesterror",
        "validationerror",
    ]

    if any(keyword in class_name for keyword in invalid_request_type_names):
        return True
    if any(keyword in error_str for keyword in invalid_request_keywords):
        return True
    return False


def is_llm_unknown_provider_error(exception: Exception) -> bool:
    """Detect if an exception is related to unknown / unsupported model provider."""
    error_str = str(exception).lower()
    class_name = type(exception).__name__.lower()

    provider_keywords = [
        "unknown provider",
        "provider not found",
        "invalid provider",
        "unsupported provider",
        "model not found",
        "model does not exist",
        "no such model",
    ]
    provider_type_names = [
        "unknownprovidererror",
        "notimplementederror",
        "notfounderror",
    ]

    if any(keyword in class_name for keyword in provider_type_names):
        return True
    if any(keyword in error_str for keyword in provider_keywords):
        return True
    return False


def is_llm_related_error(exception: Exception) -> bool:
    """Detect if an exception is likely related to LLM API calls."""
    if _is_non_llm_infrastructure_error(exception):
        return False
    return (
        is_llm_authentication_error(exception)
        or is_llm_connection_error(exception)
        or is_llm_rate_limit_error(exception)
        or is_llm_invalid_request_error(exception)
        or is_llm_unknown_provider_error(exception)
    )


def classify_llm_error(exception: Exception) -> str:
    """Classify an LLM-related error into a stable error category string."""
    if _is_non_llm_infrastructure_error(exception):
        return "unknown"
    if is_llm_authentication_error(exception):
        return "llm_authentication"
    if is_llm_unknown_provider_error(exception):
        return "llm_unknown_provider"
    if is_llm_connection_error(exception):
        return "llm_connection"
    if is_llm_rate_limit_error(exception):
        return "llm_rate_limit"
    if is_llm_invalid_request_error(exception):
        return "llm_invalid_request"
    return "unknown"


def _is_non_llm_infrastructure_error(exception: Exception) -> bool:
    module = type(exception).__module__.lower()
    class_name = type(exception).__name__.lower()
    text = f"{module}.{class_name} {exception}".lower()
    return any(
        token in text
        for token in (
            "sqlalchemy.",
            "asyncpg.",
            "psycopg",
            "integrityerror",
            "foreignkeyviolation",
            "foreign key constraint",
            "unique constraint",
        )
    )


def get_user_friendly_error_message(exception: Exception) -> str:
    """Get a user-friendly error message for the given exception."""
    error_category = classify_llm_error(exception)

    match error_category:
        case "llm_authentication" | "llm_unknown_provider":
            return "The AI service is temporarily unavailable. Please try again later."
        case "llm_connection":
            return "Unable to connect to the AI service. Please check your network and try again."
        case "llm_rate_limit":
            return (
                "The AI service is busy right now. Please try again in a few moments."
            )
        case "llm_invalid_request" | "llm_permission":
            return "The AI service is temporarily unavailable. Please try again later."
        case _:
            return "Something went wrong. Please try again or contact support if the issue persists."


def should_show_detailed_error(exception: Exception) -> bool:
    """Determine if detailed error information should be shown to the user."""
    return isinstance(exception, HTTPException)


def extract_error_context(exception: Exception) -> dict[str, Any]:
    """Extract contextual information from an exception for logging."""
    error_context = {
        "type": type(exception).__name__,
        "message": str(exception),
        "is_llm_related": is_llm_related_error(exception),
        "llm_error_category": classify_llm_error(exception),
        "user_message": get_user_friendly_error_message(exception),
    }

    if hasattr(exception, "__dict__"):
        # Sensitive key patterns to filter from logged attributes
        _SENSITIVE_PATTERNS = (
            "key",
            "token",
            "secret",
            "password",
            "credential",
            "api_key",
            "auth",
        )
        extra_attrs = {}
        for key, value in exception.__dict__.items():
            if key.startswith("_") or key in ("args", "message"):
                continue
            if any(pattern in key.lower() for pattern in _SENSITIVE_PATTERNS):
                continue
            try:
                extra_attrs[key] = str(value)
            except Exception:
                pass
        if extra_attrs:
            error_context["attributes"] = extra_attrs

    return error_context


# =============================================================================
# FastAPI Exception Handlers
# =============================================================================


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle HTTPExceptions raised by business logic."""
    assert isinstance(exc, StarletteHTTPException)

    logger.warning(
        "HTTPException: status_code=%s, detail=%s, path=%s, method=%s",
        exc.status_code,
        exc.detail,
        request.url.path,
        request.method,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=build_error_payload(
            detail=exc.detail,
            error_type="http_exception",
            error_code=f"http_{exc.status_code}",
            stage="http",
            retryable=exc.status_code in (408, 425, 429) or exc.status_code >= 500,
        ),
    )


async def validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Return validation failures in the same diagnostic envelope."""
    assert isinstance(exc, RequestValidationError)
    validation_details = [
        {
            "type": item.get("type", "validation_error"),
            "loc": list(item.get("loc", ())),
            "msg": item.get("msg", "Invalid value"),
        }
        for item in exc.errors()
    ]
    logger.warning(
        "Request validation failed: path=%s, method=%s, errors=%s",
        request.url.path,
        request.method,
        validation_details,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=build_error_payload(
            detail=validation_details,
            error_type="validation_error",
            error_code="request_validation_failed",
            stage="validation",
        ),
    )


async def llm_base_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle custom LLM-related exceptions."""
    assert isinstance(exc, LLMBaseError)

    error_context = extract_error_context(exc)

    logger.error(
        "LLM Error: type=%s, category=%s, path=%s, method=%s, message=%s",
        error_context["type"],
        error_context["llm_error_category"],
        request.url.path,
        request.method,
        error_context["message"],
        exc_info=True,
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=build_error_payload(
            detail=get_user_friendly_error_message(exc),
            error_type="llm_error",
            error_code=error_context["llm_error_category"],
            stage="llm",
            retryable=error_context["llm_error_category"]
            in ("llm_connection", "llm_rate_limit"),
        ),
    )


async def agent_hub_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle AgentHub domain errors with appropriate HTTP status codes.

    Maps domain error types to HTTP status codes:
    - AgentTimeoutError → 504 Gateway Timeout
    - LLMError → 502 Bad Gateway
    - ToolError → 500 (tool failures are internal)
    - AgentError → 500 (generic agent failures)
    - AgentHubError (base) → 500
    """
    assert isinstance(exc, AgentHubError)

    # Determine HTTP status code based on error type
    if isinstance(exc, AgentTimeoutError):
        http_status = status.HTTP_504_GATEWAY_TIMEOUT
        error_type = "agent_timeout"
    elif isinstance(exc, DomainLLMError):
        http_status = status.HTTP_502_BAD_GATEWAY
        error_type = "llm_error"
    elif isinstance(exc, DomainToolError):
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        error_type = "tool_error"
    else:
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        error_type = "agent_error"

    logger.error(
        "AgentHubError: type=%s, http_status=%d, path=%s, method=%s, message=%s",
        type(exc).__name__,
        http_status,
        request.url.path,
        request.method,
        str(exc),
        exc_info=True,
    )

    return JSONResponse(
        status_code=http_status,
        content=build_error_payload(
            detail=str(exc),
            error_type="agent_error",
            error_code=error_type,
            stage="agent",
            retryable=isinstance(exc, (AgentTimeoutError, DomainLLMError)),
        ),
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle all uncaught exceptions without exposing raw details to users."""
    error_context = extract_error_context(exc)
    is_llm_error = error_context["is_llm_related"]
    error_category = error_context["llm_error_category"]

    logger.error(
        "Uncaught Exception: type=%s, is_llm_related=%s, category=%s, path=%s, method=%s",
        error_context["type"],
        is_llm_error,
        error_category,
        request.url.path,
        request.method,
        exc_info=True,
    )

    if error_category in ("llm_authentication", "llm_unknown_provider"):
        status_code = status.HTTP_401_UNAUTHORIZED
    else:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    return JSONResponse(
        status_code=status_code,
        content=build_error_payload(
            detail=get_user_friendly_error_message(exc),
            error_type="llm_error" if is_llm_error else "internal_error",
            error_code=error_category if is_llm_error else "internal_error",
            stage="llm" if is_llm_error else "unhandled",
            retryable=error_category in ("llm_connection", "llm_rate_limit"),
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers with the FastAPI application."""
    # Starlette owns router-level 404/405 errors; FastAPI.HTTPException is its
    # subclass, so this one registration covers framework and business errors.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(LLMBaseError, llm_base_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(AgentHubError, agent_hub_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, general_exception_handler)
    logger.info("Exception handlers registered successfully")


# =============================================================================
# SSE Error Formatting
# =============================================================================


def format_sse_error(exception: Exception) -> dict[str, Any]:
    """Format an exception for SSE streaming responses.

    Ensures error messages sent through SSE streaming are user-friendly
    and don't expose sensitive information.

    Example::

        try:
            ...
        except Exception as e:
            error_data = format_sse_error(e)
            yield f"data: {json.dumps(error_data)}\\n\\n"
    """
    error_context = extract_error_context(exception)

    logger.error(
        "SSE Streaming Error: type=%s, category=%s, message=%s",
        error_context["type"],
        error_context["llm_error_category"],
        error_context["message"],
        exc_info=True,
    )

    payload = build_error_payload(
        detail=get_user_friendly_error_message(exception),
        error_type="llm_error"
        if error_context["is_llm_related"]
        else "internal_error",
        error_code=error_context["llm_error_category"]
        if error_context["is_llm_related"]
        else "internal_error",
        stage="stream",
        retryable=error_context["llm_error_category"]
        in ("llm_connection", "llm_rate_limit"),
    )
    return {
        "type": "error",
        "content": payload.pop("detail"),
        **payload,
    }


# =============================================================================
# Re-exports (preserve public API from old __init__.py)
# =============================================================================

__all__ = [
    # Exceptions
    "LLMBaseError",
    "LLMAuthenticationError",
    "LLMConnectionError",
    "LLMInvalidRequestError",
    "LLMRateLimitError",
    "LLMPermissionError",
    "LLMUnknownProviderError",
    # Error detection
    "is_llm_authentication_error",
    "is_llm_connection_error",
    "is_llm_rate_limit_error",
    "is_llm_invalid_request_error",
    "is_llm_unknown_provider_error",
    "is_llm_related_error",
    "classify_llm_error",
    # Error messages
    "get_user_friendly_error_message",
    "should_show_detailed_error",
    "extract_error_context",
    "build_error_payload",
    "resolve_request_id",
    # Exception handlers
    "http_exception_handler",
    "validation_exception_handler",
    "llm_base_error_handler",
    "general_exception_handler",
    "register_exception_handlers",
    # SSE
    "format_sse_error",
]
