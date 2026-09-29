from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Failure reasons that must never be reported as out of stock."""

    EMPTY_COMPLETED = "EMPTY_COMPLETED"
    POLL_TIMEOUT = "POLL_TIMEOUT"
    WAF_CHALLENGE = "WAF_CHALLENGE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    BATCH_SUSPECT = "BATCH_SUSPECT"
    ITEM_QUANTITY_LIMIT = "ITEM_QUANTITY_LIMIT"
    STRUCTURE_MISSING = "STRUCTURE_MISSING"
    UNEXPECTED_STATUS = "UNEXPECTED_STATUS"
    UNKNOWN_ERROR_CODE = "UNKNOWN_ERROR_CODE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


RETRYABLE = {
    ErrorCode.EMPTY_COMPLETED,
    ErrorCode.POLL_TIMEOUT,
    ErrorCode.WAF_CHALLENGE,
    ErrorCode.TRANSPORT_ERROR,
    ErrorCode.BATCH_SUSPECT,
    ErrorCode.ITEM_QUANTITY_LIMIT,
}

ITEM_STATUS_BY_CODE = {
    "INVENTORY_INSUFFICIENT": "OUT_OF_STOCK",
    "PRODUCT_NOT_BUYABLE": "NOT_CARRIED",
}


class ScanError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code
        self.message = message or code.value
