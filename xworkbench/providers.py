from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from .errors import InvalidRequestError
from .models import CollectionRequest, CollectionSummary, Post, ProviderType

BatchCallback = Callable[[list[Post], Any, dict[str, Any]], int]
CancelCallback = Callable[[], bool]


class CollectionProvider(Protocol):
    provider_id: ProviderType
    provider_version: int

    def capabilities(self) -> dict[str, Any]: ...

    def connection_status(self) -> dict[str, Any]: ...

    def prepare(
        self, request: CollectionRequest, supplied_plan: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def collect(
        self,
        request: CollectionRequest,
        *,
        execution_plan: dict[str, Any],
        checkpoint: dict[str, Any],
        on_batch: BatchCallback,
        should_cancel: CancelCallback,
    ) -> CollectionSummary: ...


def _provider_type(value: ProviderType | str) -> ProviderType:
    if str(value) == "x_api_search":
        return ProviderType.OFFICIAL_X_API
    try:
        return ProviderType(str(value))
    except ValueError as exc:
        raise InvalidRequestError(f"Unknown collection provider: {value}.") from exc


class ProviderRegistry:
    def __init__(self, providers: Iterable[CollectionProvider]):
        self._providers: dict[ProviderType, CollectionProvider] = {}
        for provider in providers:
            try:
                provider_id = _provider_type(provider.provider_id)
            except AttributeError as exc:
                raise ValueError("Collection provider is missing provider_id") from exc
            if provider_id in self._providers:
                raise ValueError(f"Duplicate collection provider: {provider_id.value}")
            self._providers[provider_id] = provider

    def get(self, provider_id: ProviderType | str) -> CollectionProvider:
        normalized = _provider_type(provider_id)
        try:
            return self._providers[normalized]
        except KeyError as exc:
            raise InvalidRequestError(
                f"Collection provider is unavailable: {normalized.value}."
            ) from exc

    def prepare(
        self, request: CollectionRequest, supplied_plan: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.get(request.provider).prepare(request, supplied_plan)

    def capabilities(self, provider_id: ProviderType | str | None = None) -> dict[str, Any]:
        if provider_id is not None:
            return self.get(provider_id).capabilities()
        return {
            key.value: provider.capabilities() for key, provider in self._providers.items()
        }

    def connection_status(
        self, provider_id: ProviderType | str | None = None
    ) -> dict[str, Any]:
        if provider_id is not None:
            return self.get(provider_id).connection_status()
        return {
            key.value: provider.connection_status() for key, provider in self._providers.items()
        }
