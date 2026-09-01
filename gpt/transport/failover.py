"""Safe conversation failover decisions (roadmap A3).

A failover deliberately *forgets* the web-side binding of a gateway
conversation (account pin, browser conversation id, bootstrap flag) so the
next turn routes to a fresh account/session from scratch. This is only safe
when the interrupted turn provably never committed on ChatGPT Web:

- ``RateLimited``            -> allowed (the send was rejected outright).
- ``AuthRequired``           -> allowed only while the record was never
                                bootstrapped on the web (nothing committed).
- ``CommitUnknown``          -> allowed ONLY when an authoritative reconcile
                                proved the user turn is absent from history.
                                Without a reconciliation verdict the decision
                                is fail-closed (no failover).

A browser conversation_id is NEVER migrated across accounts: the web context
(history, artifacts) belongs to the old account, so instead of carrying it
over we drop the binding entirely and let the next turn start a brand-new
web conversation.

This module is intentionally free of any server/gateway imports so the whole
decision table stays pure and unit-testable; callers (gateway server) supply
an optional :class:`~gpt.conversations.ConversationStore` for pending-state
cleanup and an optional callback for trace/log emission.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from gpt.state import AuthRequired, BrowserDisconnected, CommitUnknown, RateLimited

FAILOVER_ENABLED_ENV = "WEBGPT_FAILOVER_ENABLED"

# Hard cap: at most one failover per client request so A->B->A ping-pong
# between two broken accounts can never loop.
MAX_FAILOVER_PER_REQUEST = 1

_TRUTHY = {"1", "true", "yes", "on"}


class FailoverRetryRequired(BrowserDisconnected):
    """The record was failed over and the client must resend the turn.

    Subclasses :class:`BrowserDisconnected` so the existing gateway error
    mapping already reports it as HTTP 503 with ``retryable=true`` /
    ``x-should-retry: true``, prompting a clean client-side resend without
    leaking the original internal exception.
    """


def failover_enabled() -> bool:
    """Resolve the WEBGPT_FAILOVER_ENABLED switch (default: enabled)."""
    return os.environ.get(FAILOVER_ENABLED_ENV, "1").strip().lower() in _TRUTHY


def _clear_pending_fields(record: Any) -> None:
    record.pending_request_fingerprint = None
    record.pending_prompt = None
    record.pending_submitted_at = None


def maybe_failover(
    record: Any,
    exc: BaseException,
    *,
    enabled: bool = True,
    attempts: int = 0,
    reconciled_user_turn_present: bool | None = None,
    store: Any = None,
    emit: Callable[[str], None] | None = None,
) -> bool:
    """Decide whether ``exc`` justifies failing ``record`` over to a fresh
    account/web session, and if so reset its routing state in place.

    Parameters
    ----------
    record:
        The live :class:`~gpt.conversations.ConversationRecord`.
    exc:
        The exception raised by the failed turn execution.
    enabled:
        Caller-level kill switch; combined with the process-wide
        ``WEBGPT_FAILOVER_ENABLED`` environment flag (default on).
    attempts:
        How many failovers this request already performed. The cap of one
        makes the second call always return ``False``.
    reconciled_user_turn_present:
        Reconciliation verdict for ``CommitUnknown``. ``None`` (unknown or
        unavailable) fails closed; only an explicit ``False`` — history
        proves the user turn never landed — permits the reset.
    store:
        Optional :class:`~gpt.conversations.ConversationStore`; when given
        and a pending send is recorded, ``store.clear_pending`` is used so
        persistence stays through the store's own API.
    emit:
        Optional callback invoked with the reason string once a failover is
        applied (for trace/log emission by the caller).

    Returns True only when the record was actually reset for failover.
    """
    if not enabled or not failover_enabled():
        return False
    if attempts >= MAX_FAILOVER_PER_REQUEST:
        return False

    reason: str | None = None
    if isinstance(exc, RateLimited):
        # The send was rejected before generation; nothing could have commit.
        reason = "rate_limited"
    elif isinstance(exc, AuthRequired):
        # Only safe before the record ever bootstrapped a web conversation;
        # after that the account context exists and must not be discarded.
        if not record.web_bootstrapped:
            reason = "auth_required"
    elif isinstance(exc, CommitUnknown):
        submitted = bool(getattr(exc, "submitted", True))
        if not submitted:
            # The runtime asserts the send never happened.
            reason = "commit_unknown_not_submitted"
        elif reconciled_user_turn_present is False:
            # Authoritative history proves the user turn is absent.
            reason = "commit_unknown_reconciled_absent"
        # else: unknown verdict -> fail closed, no failover.

    if reason is None:
        return False

    record.account_name = None
    record.conversation_id = None
    record.web_bootstrapped = False
    if store is not None:
        if record.pending_request_fingerprint is not None:
            store.clear_pending(record)
    elif record.pending_request_fingerprint is not None:
        _clear_pending_fields(record)
    if emit is not None:
        emit(reason)
    return True


__all__ = [
    "FAILOVER_ENABLED_ENV",
    "MAX_FAILOVER_PER_REQUEST",
    "FailoverRetryRequired",
    "failover_enabled",
    "maybe_failover",
]
