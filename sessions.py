"""
Per-WhatsApp-user session state (in-memory).

Works for a single server process (e.g. one Render instance). For multiple
instances, replace with Redis or another shared store.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("whatsapp-bot")

SESSION_TTL = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UserSession:
    wa_id: str
    updated_at: datetime = field(default_factory=_utcnow)
    message_count: int = 0
    last_flow: str = ""


_sessions: dict[str, UserSession] = {}
_lock = threading.Lock()


def touch(wa_id: str, flow: str = "") -> UserSession:
    """Update or create a session for this WhatsApp user."""
    if not wa_id:
        raise ValueError("wa_id required")

    with _lock:
        _prune_stale_unlocked()
        s = _sessions.get(wa_id)
        if s is None:
            s = UserSession(wa_id=wa_id)
            _sessions[wa_id] = s
        s.updated_at = _utcnow()
        s.message_count += 1
        if flow:
            s.last_flow = flow
        logger.info(
            "session       | wa_id=%s msgs=%s flow=%s",
            wa_id,
            s.message_count,
            s.last_flow or "-",
        )
        return s


def _prune_stale_unlocked() -> None:
    """Remove sessions idle longer than SESSION_TTL (must hold _lock)."""
    cutoff = _utcnow() - SESSION_TTL
    stale = [k for k, v in _sessions.items() if v.updated_at < cutoff]
    for k in stale:
        del _sessions[k]
    if stale:
        logger.info("session       | pruned %s idle session(s)", len(stale))


def snapshot_stats() -> dict:
    """Lightweight stats for monitoring (protect with INTERNAL_API_KEY)."""
    with _lock:
        _prune_stale_unlocked()
        return {
            "active_sessions": len(_sessions),
            "ttl_hours": SESSION_TTL.total_seconds() / 3600,
        }


def get_session(wa_id: str) -> UserSession | None:
    with _lock:
        return _sessions.get(wa_id)
