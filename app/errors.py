"""Typed application errors so the API can map them to clean HTTP responses."""


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


class ConfigError(AppError):
    status_code = 503
    code = "config_error"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ExtractionError(AppError):
    status_code = 502
    code = "extraction_failed"


class UpstreamError(AppError):
    status_code = 502
    code = "upstream_error"
