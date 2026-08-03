from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..models import CollectionRequest, CollectionSummary, Tweet

BatchCallback = Callable[[list[Tweet], str | None, dict[str, Any], int], int]
CancelCallback = Callable[[], bool]


class CollectionProvider(Protocol):
    def collect(
        self,
        request: CollectionRequest,
        *,
        cursor: str | None,
        cursor_context: dict[str, Any] | None,
        on_batch: BatchCallback,
        should_cancel: CancelCallback,
    ) -> CollectionSummary: ...

    def session_status(self) -> dict[str, str | bool]: ...
