from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ..models import CollectionRequest, CollectionSummary, Tweet

BatchCallback = Callable[[list[Tweet], str | None], None]
CancelCallback = Callable[[], bool]


class CollectionProvider(Protocol):
    def collect(
        self,
        request: CollectionRequest,
        *,
        cursor: str | None,
        on_batch: BatchCallback,
        should_cancel: CancelCallback,
    ) -> CollectionSummary: ...

    def session_status(self) -> dict[str, str | bool]: ...
