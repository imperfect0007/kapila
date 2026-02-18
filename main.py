import logging
import sys

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import PlainTextResponse
import httpx

load_dotenv(".env.local")

# ──────────────────────────────────────────────
# Logging – structured output visible in Render
# ──────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("whatsapp-bot")

# ──────────────────────────────────────────────
# Constants – loaded from .env.local
# ──────────────────────────────────────────────
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")

GRAPH_API_URL = (
    f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
)

app = FastAPI(title="WhatsApp Enquiry Bot")


# ──────────────────────────────────────────────
# Rule-based reply generator
# ──────────────────────────────────────────────
def generate_reply(text: str) -> str:
    """Return a reply based on simple keyword matching."""
    text_lower = text.lower()

    if "hi" in text_lower or "hello" in text_lower:
        return (
            "Welcome to Kapila Resort! 🏨\n"
            "We're happy to help you.\n\n"
            "You can ask about:\n"
            "• Room availability – type *room*\n"
            "• Pricing – type *price*\n"
            "• Or just ask your question!"
        )

    if "price" in text_lower or "cost" in text_lower or "rate" in text_lower:
        return (
            "Our current rates:\n\n"
            "🛏 Standard Room – ₹2,500/night\n"
            "🛏 Deluxe Room   – ₹4,000/night\n"
            "🛏 Suite         – ₹7,500/night\n\n"
            "All rates include breakfast. "
            "Type *room* to check availability."
        )

    if "room" in text_lower or "available" in text_lower or "book" in text_lower:
        return (
            "We currently have rooms available! ✅\n\n"
            "• Standard Room – Available\n"
            "• Deluxe Room   – Available\n"
            "• Suite         – Limited\n\n"
            "To book, please call us at +91-XXXXX-XXXXX "
            "or reply with your preferred dates."
        )

    return (
        "Thank you for reaching out! 🙏\n\n"
        "I can help you with:\n"
        "• Say *hi* for a welcome message\n"
        "• Say *price* for room rates\n"
        "• Say *room* to check availability\n\n"
        "Or type your question and we'll get back to you shortly."
    )


# ──────────────────────────────────────────────
# Send a WhatsApp message via the Graph API
# ──────────────────────────────────────────────
async def send_message(to: str, message: str) -> None:
    """Send a text message through the Meta WhatsApp Cloud API."""
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                GRAPH_API_URL, headers=headers, json=payload
            )
            logger.info("send_message  | to=%s | status=%s", to, response.status_code)
            logger.info("send_message  | response=%s", response.text)
        except httpx.RequestError as exc:
            logger.error("send_message  | request failed: %s", exc)


# ──────────────────────────────────────────────
# Webhook verification (GET)
# ──────────────────────────────────────────────
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """
    Meta sends a GET request with hub.mode, hub.verify_token, and
    hub.challenge to verify the webhook URL.
    """
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("verify        | webhook verified successfully")
        return PlainTextResponse(content=hub_challenge)

    logger.warning("verify        | verification failed – token mismatch")
    raise HTTPException(status_code=403, detail="Verification failed")


# ──────────────────────────────────────────────
# Webhook receiver (POST)
# ──────────────────────────────────────────────
@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receives incoming messages from WhatsApp, generates a reply,
    and sends it back to the sender.
    """
    body = await request.json()
    logger.info("webhook       | incoming payload: %s", body)

    try:
        entry = body.get("entry", [])
        for e in entry:
            changes = e.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])

                for msg in messages:
                    sender = msg.get("from")
                    text = msg.get("text", {}).get("body", "")

                    logger.info("webhook       | from=%s | text=%s", sender, text)

                    reply = generate_reply(text)
                    await send_message(sender, reply)

                    logger.info("webhook       | replied to=%s", sender)

    except Exception as exc:
        logger.exception("webhook       | error processing message: %s", exc)

    return {"status": "ok"}
