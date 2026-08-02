from __future__ import annotations


class ScraperError(Exception):
    code = "scraper_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None):
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class InvalidRequestError(ScraperError):
    code = "invalid_request"


class SessionMissingError(ScraperError):
    code = "session_missing"


class SessionExpiredError(ScraperError):
    code = "session_expired"


class ProfileUnavailableError(ScraperError):
    code = "profile_unavailable"


class RateLimitedError(ScraperError):
    code = "rate_limited"
    retryable = True


class CollectionTimeoutError(ScraperError):
    code = "collection_timeout"
    retryable = True


class SchemaDriftError(ScraperError):
    code = "schema_drift"
    retryable = True


class CollectionCancelled(ScraperError):
    code = "cancelled"
    retryable = True
