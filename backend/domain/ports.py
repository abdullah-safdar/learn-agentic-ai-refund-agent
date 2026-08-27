"""Port interfaces the domain depends on (AD-1).

The domain core imports only these interfaces. Concrete implementations live
in `adapters/` and import *this* module -- never the other way around.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from domain.models import RefundRequest


@dataclass(frozen=True)
class ExtractedRefundFields:
    """The fixed extraction schema (Intent -> Approach, Boundaries & Constraints).

    `order_reference` and `reason` are always strings -- an empty string
    means the field was not stated in the customer's message. `amount_cents`
    may be null (`None`) if unstated; when present it is integer cents, never
    a float.
    """

    order_reference: str
    reason: str
    amount_cents: Optional[int]


class LLMPort(ABC):
    """Outbound port for turning free-text chat into the fixed extraction
    schema. Swappable: no LLM provider/SDK is architecturally mandated.
    """

    @abstractmethod
    def extract_refund_request(self, chat_text: str) -> ExtractedRefundFields:
        """Extract `{order_reference, reason, amount_cents}` from a single
        customer chat message. Implementations must never raise for
        merely-ambiguous input -- return empty strings / None for fields
        that aren't stated rather than guessing or failing.
        """
        raise NotImplementedError


class RefundRepositoryPort(ABC):
    """Outbound port for persisting/looking-up RefundRequest rows.

    Adapters implementing this port perform no dedup logic of their own
    (AD-3) -- they only translate `find_by_dedup_key`/`save` into storage
    operations keyed on the value the domain already computed.
    """

    @abstractmethod
    def find_by_dedup_key(self, dedup_key: str) -> Optional[RefundRequest]:
        """Return the RefundRequest previously saved under this exact
        intake-dedup key, or None if no such row exists."""
        raise NotImplementedError

    @abstractmethod
    def save(self, refund_request: RefundRequest, dedup_key: str) -> bool:
        """Persist a newly created RefundRequest under the given intake-dedup
        key. Implementations must treat a collision on `dedup_key` as a
        no-op (idempotent write) rather than an error, since a concurrent
        duplicate submission may race the domain's own check-then-create.

        Returns True if this call actually inserted a new row, False if a
        row already existed under `dedup_key` (the no-op case). The domain
        relies on this to detect when it lost a concurrent insert race --
        on False, it re-fetches and returns the row that actually won,
        never the locally-constructed one that didn't get persisted.
        """
        raise NotImplementedError
