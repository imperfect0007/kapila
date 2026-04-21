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

# WhatsApp “Get callback” multi-step flow (per wa_id)
CALLBACK_IDLE = "idle"
CALLBACK_MODE_ROOM = "room_booking"
CALLBACK_MODE_VENUE = "venue_booking"
CALLBACK_MODE_DAYOUT = "day_out"
CALLBACK_MODE_BOOKING = "booking"  # legacy alias
CALLBACK_MODE_PROPERTY_VISIT = "property_visit"

CALLBACK_NAME = "name"
CALLBACK_PHONE = "phone"
CALLBACK_CHECKIN = "checkin"
CALLBACK_CHECKOUT = "checkout"
CALLBACK_PACKS = "packs"
CALLBACK_PROPERTY_FINALIZE = "property_finalize"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UserSession:
    wa_id: str
    updated_at: datetime = field(default_factory=_utcnow)
    message_count: int = 0
    last_flow: str = ""
    callback_step: str = CALLBACK_IDLE
    callback_mode: str = CALLBACK_MODE_BOOKING
    cb_name: str = ""
    cb_phone: str = ""
    cb_checkin: str = ""  # YYYY-MM-DD
    cb_checkout: str = ""


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


def callback_step_for(wa_id: str) -> str:
    with _lock:
        s = _sessions.get(wa_id)
        return s.callback_step if s else CALLBACK_IDLE


def callback_mode_for(wa_id: str) -> str:
    with _lock:
        s = _sessions.get(wa_id)
        return s.callback_mode if s else CALLBACK_MODE_BOOKING


def _callback_reset_unlocked(s: UserSession) -> None:
    s.callback_step = CALLBACK_IDLE
    s.callback_mode = CALLBACK_MODE_BOOKING
    s.cb_name = s.cb_phone = s.cb_checkin = s.cb_checkout = ""


def callback_abort(wa_id: str) -> None:
    with _lock:
        s = _sessions.get(wa_id)
        if s:
            _callback_reset_unlocked(s)
            s.updated_at = _utcnow()


def callback_begin(wa_id: str, mode: str = CALLBACK_MODE_BOOKING) -> None:
    with _lock:
        _prune_stale_unlocked()
        s = _sessions.get(wa_id)
        if s is None:
            s = UserSession(wa_id=wa_id)
            _sessions[wa_id] = s
        s.callback_step = CALLBACK_NAME
        s.callback_mode = mode
        s.cb_name = s.cb_phone = s.cb_checkin = s.cb_checkout = ""
        s.updated_at = _utcnow()


def callback_after_name(wa_id: str, name: str) -> None:
    with _lock:
        s = _sessions.get(wa_id)
        if not s:
            return
        s.cb_name = name
        s.callback_step = CALLBACK_PHONE
        s.updated_at = _utcnow()


def callback_after_phone(wa_id: str, phone: str) -> None:
    with _lock:
        s = _sessions.get(wa_id)
        if not s:
            return
        s.cb_phone = phone
        if s.callback_mode == CALLBACK_MODE_PROPERTY_VISIT:
            s.callback_step = CALLBACK_PROPERTY_FINALIZE
        else:
            s.callback_step = CALLBACK_CHECKIN
        s.updated_at = _utcnow()


def callback_after_checkin(wa_id: str, checkin_iso: str) -> None:
    with _lock:
        s = _sessions.get(wa_id)
        if not s:
            return
        s.cb_checkin = checkin_iso
        if s.callback_mode == CALLBACK_MODE_DAYOUT:
            # Day-out is a single-day flow: no separate check-out step.
            s.cb_checkout = ""
            s.callback_step = CALLBACK_PACKS
        else:
            s.callback_step = CALLBACK_CHECKOUT
        s.updated_at = _utcnow()


def callback_after_checkout(wa_id: str, checkout_iso: str) -> None:
    with _lock:
        s = _sessions.get(wa_id)
        if not s:
            return
        s.cb_checkout = checkout_iso
        s.callback_step = CALLBACK_PACKS
        s.updated_at = _utcnow()


def callback_get_checkin_iso(wa_id: str) -> str:
    with _lock:
        s = _sessions.get(wa_id)
        return s.cb_checkin if s else ""


def callback_make_record(wa_id: str, packs: int) -> dict | None:
    """If in packs step, build enquiry payload (does not clear state)."""
    with _lock:
        s = _sessions.get(wa_id)
        if not s or s.callback_step != CALLBACK_PACKS:
            return None
        source_map = {
            CALLBACK_MODE_ROOM: "whatsapp_room_booking",
            CALLBACK_MODE_VENUE: "whatsapp_venue_booking",
            CALLBACK_MODE_DAYOUT: "whatsapp_day_out",
        }
        return {
            "name": s.cb_name,
            "phone": s.cb_phone,
            "check_in": s.cb_checkin,
            "check_out": s.cb_checkout,
            "packs": packs,
            "source": source_map.get(s.callback_mode, "whatsapp_callback"),
        }


def callback_make_property_visit_record(wa_id: str) -> dict | None:
    """Build an enquiry payload for property visits (does not clear state)."""
    with _lock:
        s = _sessions.get(wa_id)
        if not s or s.callback_mode != CALLBACK_MODE_PROPERTY_VISIT:
            return None
        return {
            "name": s.cb_name,
            "phone": s.cb_phone,
            "visit_purpose": "Property visit",
            # Values from your requirement:
            "fnq": 1,
            "rooms": 5,
            "occupancy": 15,
            "tent_extra_packs": 2,
            "walkin_available": False,
            "source": "whatsapp_property_visit",
        }


def callback_clear_flow(wa_id: str) -> None:
    """Clear callback state after a successful save."""
    with _lock:
        s = _sessions.get(wa_id)
        if s:
            _callback_reset_unlocked(s)
            s.updated_at = _utcnow()
