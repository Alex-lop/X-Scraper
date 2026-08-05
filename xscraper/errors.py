from __future__ import annotations


class CollectionError(Exception):
    code = "provider_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None):
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class InvalidRequestError(CollectionError):
    code = "invalid_request"


class CredentialError(CollectionError):
    code = "credential_or_access_failure"


class BillingError(CollectionError):
    code = "billing_failure"


class RateLimitWaiting(CollectionError):
    code = "rate_limited"
    retryable = True

    def __init__(self, message: str, retry_at: str, remaining: int | None, reset: int | None):
        super().__init__(message)
        self.retry_at = retry_at
        self.remaining = remaining
        self.reset = reset


class NetworkError(CollectionError):
    code = "network_failure"
    retryable = True


class SchemaDriftError(CollectionError):
    code = "schema_mismatch"


class ResumeIncompatibleError(CollectionError):
    code = "resume_incompatible"


class CollectionCancelled(CollectionError):
    code = "cancelled"
    retryable = True
