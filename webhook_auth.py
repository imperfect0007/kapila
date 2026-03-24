"""
Meta WhatsApp Cloud API webhook signature verification (HMAC SHA-256).

Set APP_SECRET in the environment (same as Meta App Dashboard → App Secret).
If unset, verification is skipped (local dev only).
"""
import hashlib
import hmac
import logging
import os

logger = logging.getLogger("whatsapp-bot")

APP_SECRET = os.getenv("APP_SECRET", "").strip()


def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """
    Validate X-Hub-Signature-256 from Meta.
    Header format: sha256=<hex_digest>
    """
    if not APP_SECRET:
        logger.warning(
            "auth          | APP_SECRET not set – webhook signature check disabled "
            "(set for production)"
        )
        return True

    if not signature_header:
        logger.warning("auth          | missing X-Hub-Signature-256")
        return False

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        logger.warning("auth          | bad signature header format")
        return False

    expected = hmac.new(
        APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    received = signature_header[len(prefix) :]

    if not hmac.compare_digest(expected, received):
        logger.warning("auth          | signature mismatch")
        return False

    return True
