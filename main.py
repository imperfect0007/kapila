import asyncio
import html
import json
import logging
import re
import sys
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, HTTPException, Header, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import httpx

import sessions
import webhook_auth

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
# Meta App Secret – used to verify POST /webhook (X-Hub-Signature-256)
# Optional: INTERNAL_API_KEY for GET /stats (session counts)
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "").strip()

GRAPH_API_URL = (
    f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
)

# Meta Cloud API: interactive reply buttons — max 3, title max 20 chars each.
WHATSAPP_MAX_REPLY_BUTTONS = 3
WHATSAPP_MAX_BUTTON_TITLE_LEN = 20

# Comma-separated WhatsApp numbers (digits only, country code, no +) to receive
# enquiry alerts (name, phone, dates, guests) so desk staff can call back.
# Example: DESK_NOTIFY_WHATSAPP=919108138510,918214001100
# Delivery follows Meta rules (recipient may need an open chat with your business).
DESK_NOTIFY_WHATSAPP = os.getenv("DESK_NOTIFY_WHATSAPP", "").strip()

# ── Guest contact (tap numbers in WhatsApp to call) ──
CONTACT_NAME = "Kavitha"
PHONE_KAVITHA = "+919108138510"
PHONE_KAVITHA_ALT = "+919606654482"
PHONE_RECEPTION_24_7 = "+918214001100"

CONTACT_PHONE_LINES = (
    f"📞 *{CONTACT_NAME} (primary):* {PHONE_KAVITHA}\n"
    f"📞 *Alternate:* {PHONE_KAVITHA_ALT}\n"
    f"📞 *24/7 reception:* {PHONE_RECEPTION_24_7}"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PHOTOS_ROOT = os.path.join(BASE_DIR, "photos")
DATA_DIR = os.path.join(BASE_DIR, "data")
ENQUIRY_LOG = os.path.join(DATA_DIR, "enquiries.jsonl")

# Public HTTPS base for WhatsApp image links (e.g. https://your-app.onrender.com)
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    or os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def list_gallery_filenames(category: str) -> list[str]:
    """Return sorted image filenames under photos/<category>/."""
    folder = os.path.join(PHOTOS_ROOT, category)
    if not os.path.isdir(folder):
        return []
    names = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and os.path.splitext(name.lower())[1] in _IMAGE_EXTS:
            names.append(name)
    return names


def public_photo_url(category: str, filename: str) -> str:
    """Build absolute URL for a file served at /photos/<category>/<filename>."""
    return f"{PUBLIC_BASE_URL}/photos/{category}/{quote(filename)}"


RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "")
PING_INTERVAL = 14 * 60  # 14 minutes


# ──────────────────────────────────────────────
# Self-ping to keep Render awake
# ──────────────────────────────────────────────
async def keep_alive() -> None:
    """Ping our own /ping endpoint every 14 minutes so Render doesn't sleep."""
    if not RENDER_URL:
        logger.warning("keep_alive    | RENDER_EXTERNAL_URL not set – self-ping disabled")
        return

    url = f"{RENDER_URL}/ping"
    logger.info("keep_alive    | will ping %s every %ss", url, PING_INTERVAL)

    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=10)
                logger.info("keep_alive    | pinged %s – status %s", url, resp.status_code)
        except Exception as exc:
            logger.error("keep_alive    | ping failed: %s", exc)


@asynccontextmanager
async def lifespan(application: FastAPI):
    task = asyncio.create_task(keep_alive())
    yield
    task.cancel()


app = FastAPI(title="WhatsApp Enquiry Bot", lifespan=lifespan)

if os.path.isdir(PHOTOS_ROOT):
    app.mount("/photos", StaticFiles(directory=PHOTOS_ROOT), name="photos")
else:
    logger.warning("photos        | folder missing: %s", PHOTOS_ROOT)


@app.get("/ping")
async def ping():
    """Health-check endpoint used by the self-ping task and uptime monitors."""
    return {"status": "alive"}


@app.get("/stats")
async def session_stats(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None),
):
    """
    Optional session metrics (active in-memory sessions).
    Set INTERNAL_API_KEY in the environment, then call with header:
    X-API-Key: <key>  OR  Authorization: Bearer <key>
    """
    if not INTERNAL_API_KEY:
        raise HTTPException(status_code=404, detail="Not found")
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token or token != INTERNAL_API_KEY:
        logger.warning("stats         | unauthorized access attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return sessions.snapshot_stats()


# ──────────────────────────────────────────────
# Web enquiry form (W3C HTML5) — submissions stored as JSON Lines
# ──────────────────────────────────────────────
def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, mode=0o755, exist_ok=True)


def append_enquiry_record(payload: dict) -> None:
    _ensure_data_dir()
    payload["received_at"] = datetime.now(timezone.utc).isoformat()
    with open(ENQUIRY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    logger.info(
        "enquiry       | saved name=%s check_in=%s packs=%s",
        payload.get("name"),
        payload.get("check_in"),
        payload.get("packs"),
    )


def _enquiry_form_error(message: str, status_code: int = 400) -> HTMLResponse:
    safe = html.escape(message)
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enquiry error</title>
<style>body{{font-family:Georgia,serif;max-width:28rem;margin:2rem auto;padding:1rem;color:#1c1914;}}
a{{color:#2d5a3d}}</style></head><body>
<p><strong>Could not submit.</strong> {safe}</p>
<p><a href="/enquiry">← Back to form</a></p>
</body></html>""",
        status_code=status_code,
    )


def _enquiry_thank_you() -> HTMLResponse:
    return HTMLResponse(
        """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thank you — Kapila River Front</title>
<style>body{font-family:Georgia,serif;max-width:28rem;margin:2rem auto;padding:1rem;color:#1c1914;}
h1{font-size:1.35rem;} a{color:#2d5a3d}</style></head><body>
<h1>Thank you!</h1>
<p>We’ve received your enquiry. Our team will contact you shortly on the phone number you provided.</p>
<p><a href="/enquiry">Submit another enquiry</a></p>
</body></html>"""
    )


@app.get("/", response_class=HTMLResponse)
async def root_page():
    """Landing page with link to the enquiry form."""
    base = PUBLIC_BASE_URL
    enquiry_href = f"{base}/enquiry" if base else "/enquiry"
    safe_href = html.escape(enquiry_href, quote=True)
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kapila River Front</title>
<style>body{{font-family:Georgia,serif;max-width:28rem;margin:3rem auto;padding:1.5rem;text-align:center;color:#1c1914;}}
a{{display:inline-block;margin-top:1rem;color:#2d5a3d;font-weight:600}}</style></head><body>
<h1>Kapila River Front</h1>
<p>WhatsApp bot &amp; booking enquiry.</p>
<p><a href="{safe_href}">Open booking enquiry form</a></p>
</body></html>"""
    )


@app.get("/enquiry", response_class=HTMLResponse)
async def enquiry_form_get():
    """Serve the HTML5 booking enquiry form."""
    path = os.path.join(BASE_DIR, "static", "enquiry.html")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Form not found")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/enquiry", response_class=HTMLResponse)
async def enquiry_form_post(
    name: str = Form(...),
    phone: str = Form(...),
    check_in: str = Form(...),
    check_out: str = Form(...),
    packs: int = Form(...),
):
    """Accept form POST, validate, append one JSON line per submission."""
    name = (name or "").strip()
    phone = (phone or "").strip()
    if len(name) < 2 or len(name) > 120:
        return _enquiry_form_error("Please enter a valid name (2–120 characters).")
    if len(phone) < 8 or len(phone) > 20:
        return _enquiry_form_error("Please enter a valid phone number.")

    try:
        d_in = date.fromisoformat(check_in)
        d_out = date.fromisoformat(check_out)
    except ValueError:
        return _enquiry_form_error("Invalid check-in or check-out date.")

    if d_out < d_in:
        return _enquiry_form_error("Check-out must be on or after check-in.")

    if packs < 1 or packs > 30:
        return _enquiry_form_error("Guests (packs) must be between 1 and 30.")

    record = {
        "name": name,
        "phone": phone,
        "check_in": check_in,
        "check_out": check_out,
        "packs": packs,
        "source": "web_form",
    }
    try:
        append_enquiry_record(record)
    except OSError as exc:
        logger.exception("enquiry       | failed to save: %s", exc)
        return _enquiry_form_error(
            "We could not save your enquiry. Please try again or call us.",
            status_code=500,
        )

    await notify_desk_staff(record, "Website form")
    return _enquiry_thank_you()


# ──────────────────────────────────────────────
# Rule-based reply generator
# ──────────────────────────────────────────────
def generate_reply(text: str) -> str:
    """Return a reply based on keyword matching against real Kapila resort info."""
    t = text.lower()

    # ── Greeting ──
    if any(w in t for w in ("hi", "hello", "hey", "hii")):
        return (
            "Welcome to *Kapila River Front*! 🌿🏨\n"
            "A Luxury Farm Villa on the Riverside\n\n"
            "Here's what I can help you with:\n"
            "📸 *gallery* – Photo gallery (indoor / outdoor / activities)\n"
            "🛏 *room* – Room details\n"
            "💰 *price* – 2026 Rate card\n"
            "📅 *book* – Booking enquiry\n"
            "🐾 *pet* – Pet policy & charges\n"
            "🎯 *activities* – Sports & games\n"
            "🌿 *amenities* – Facilities\n"
            "🍽 *food* – Dining & meals\n"
            "📍 *location* – How to reach us\n"
            "❌ *cancel* – Cancellation policy\n"
            "🏦 *payment* – Bank & payment info\n"
            "📋 *menu* – Full keyword list\n"
            "📞 *call* / *kavitha* – Phone numbers & contact card\n"
            + (
                f"📝 *Web form:* {PUBLIC_BASE_URL}/enquiry\n\n"
                if PUBLIC_BASE_URL
                else ""
            )
            + "Or use the buttons — tap *Room Booking* for a booking enquiry 📞\n\n"
            "Or just type your question! 😊"
        )

    # ── Pricing / Rate card ──
    if any(w in t for w in ("price", "cost", "rate", "tariff", "charge", "fee")):
        return (
            "💰 *Kapila River Front – 2026 Rate Card*\n\n"
            "All rates are *per room, per night* for "
            "*double occupancy*, *inclusive of all meals* "
            "(welcome drinks, lunch, high tea, dinner & breakfast).\n\n"
            "📌 *Regular (Non-Seasonal):*\n"
            "• Weekdays: *₹10,000*\n"
            "• Weekends / Holidays: *₹12,000*\n\n"
            "📌 *March – May:*\n"
            "• Weekdays: *₹12,000*\n"
            "• Weekends: *₹13,000*\n\n"
            "📌 *Dasara (10-day festival):*\n"
            "• All days: *₹13,000*\n\n"
            "📌 *December 1–15:*\n"
            "• All days: *₹13,000*\n\n"
            "📌 *December 15–29:*\n"
            "• All days: *₹14,000*\n\n"
            "📌 *January 2 – First weekend:*\n"
            "• All days: *₹14,000*\n\n"
            "📌 *January 5–15:*\n"
            "• All days: *₹12,000*\n\n"
            "📌 *Valentine's Day (14 Feb):*\n"
            "• *₹14,000* per night\n\n"
            "Type *newyear* for the NYE special package!\n"
            "Type *pet* for pet charges or *book* to enquire."
        )

    # ── New Year special package ──
    if any(w in t for w in ("new year", "newyear", "nye", "31 dec", "31st dec",
                             "30 dec", "30th dec", "1 jan", "1st jan", "gala")):
        return (
            "🎆 *New Year Special Package 2026*\n\n"
            "✨ *Mandatory Full Property Booking*\n"
            "📅 2 Nights / 3 Days\n\n"
            "*Option 1:*\n"
            "• Check-in: 30th December\n"
            "• Check-out: 1st January\n\n"
            "*Option 2:*\n"
            "• Check-in: 31st December\n"
            "• Check-out: 2nd January\n\n"
            "💰 *Total Package: ₹2,25,000*\n\n"
            "✅ *Includes:*\n"
            "• All 5 rooms (10 pax)\n"
            "• All meals included\n"
            "• Firecrackers\n"
            "• *New Year Gala Dinner with Barbecue* 🥂\n\n"
            "⚠️ Non-divisible – must be booked as "
            "a full property buyout.\n\n"
            "Type *book* to enquire or *price* for the full rate card."
        )

    # ── Room details ──
    if any(w in t for w in ("room", "bed", "stay", "accommodation", "villa")):
        return (
            "🏨 *Kapila River Front – Room Details*\n\n"
            "We have *5 identical Heritage Rooms*.\n\n"
            "✨ *Room highlights:*\n"
            "• Spacious high-ceiling interior with warm wooden furnishings\n"
            "• Handcrafted wooden bed with elegant ambient lighting\n"
            "• Patterned flooring & tasteful wall art\n"
            "• Private balcony sit-out with comfortable seating\n"
            "• Large glass doors – seamless indoor-outdoor flow\n"
            "• Attached modern washroom with modern fittings\n\n"
            "🔌 *In-room facilities:*\n"
            "• Air Conditioning (A/C)\n"
            "• Television (TV)\n"
            "• Hot water kettle\n\n"
            "👥 *Occupancy:*\n"
            "• Min 2 / Max 3 guests per room\n"
            "• Total: 5 rooms → up to 15 guests (with extra beds)\n\n"
            "ℹ️ One room type only. No river-facing view.\n\n"
            "Type *price* for rates or *book* to enquire!"
        )

    # ── Booking enquiry ──
    if any(w in t for w in ("book", "reserve", "checkin", "check-in",
                             "checkout", "check-out")):
        form_extra = ""
        if PUBLIC_BASE_URL:
            form_extra = f"📝 *Submit details online:*\n{PUBLIC_BASE_URL}/enquiry\n\n"
        return (
            "📅 *Booking Enquiry*\n\n"
            "We'd love to host you at Kapila River Front! 🌿\n\n"
            f"{form_extra}"
            "🕐 *Check-in:* 1:00 PM\n"
            "🕚 *Check-out:* 11:00 AM\n\n"
            "Please share:\n"
            "1️⃣ Check-in date\n"
            "2️⃣ Check-out date\n"
            "3️⃣ Number of guests\n"
            "4️⃣ Number of rooms needed\n"
            "5️⃣ Traveling with pets? (Yes/No)\n\n"
            f"📞 Or call us:\n{CONTACT_PHONE_LINES}\n\n"
            "✅ Booking is confirmed only after *100% payment*.\n"
            "Type *cancel* for cancellation policy.\n"
            "Type *payment* for bank details."
        )

    # ── Pet policy ──
    if any(w in t for w in ("pet", "dog", "cat", "puppy", "animal")):
        return (
            "🐾 *Pet Policy – Kapila River Front*\n\n"
            "Yes! We *welcome pets* and allow them *inside rooms*. 🐶\n\n"
            "📌 *Pet Limits:*\n"
            "• Max *2 pets per room*\n"
            "• Max *6 pets across all 5 rooms* "
            "(if one or more are small breeds like Shih Tzu)\n\n"
            "💰 *Pet Charges:*\n"
            "• *₹2,000 per pet* – includes boiled vegetables & cooked rice\n"
            "• *₹500 extra per pet* – for chicken add-on 🍗\n\n"
            "⚠️ *Guidelines:*\n"
            "• Inform the reservation team *in advance*\n"
            "• Pets must be *leashed if not fully trained*\n"
            "• Owners are *fully responsible* for pet behavior\n"
            "• The property is *open to the riverfront* with no barricading "
            "– please *supervise pets closely* near the river\n"
            "• Any damage or extra cleaning will be *charged to the guest*\n\n"
            "Type *book* to make a reservation or *price* for rates."
        )

    # ── Cancellation policy ──
    if any(w in t for w in ("cancel", "cancellation", "refund", "policy")):
        return (
            "❌ *Cancellation Policy*\n\n"
            "✅ Booking is confirmed only after *100% payment*.\n\n"
            "📌 *Refund rules:*\n"
            "• *15+ days* before check-in → *Full refund* (free cancellation)\n"
            "• *14–15 days* before → *25% deducted*\n"
            "• *10 days* before → *50% deducted*\n"
            "• *Less than 7 days* → *No refund*\n\n"
            "For any changes to your booking, please contact us:\n"
            f"{CONTACT_PHONE_LINES}"
        )

    # ── Payment / bank details ──
    if any(w in t for w in ("payment", "pay", "bank", "account", "upi",
                             "transfer", "neft", "imps", "ifsc")):
        return (
            "🏦 *Payment Details*\n\n"
            "Please transfer to the following account:\n\n"
            "🏛 *Bank:* CANARA BANK\n"
            "👤 *Account Name:* KAPILA RIVER FRONT\n"
            "🔢 *Account Number:* 120032425830\n"
            "🏷 *IFSC Code:* CNRB0002655\n"
            "📍 *Branch:* Ramakrishna Nagar, Mysore\n\n"
            "✅ Booking is confirmed only after *100% payment*.\n\n"
            "After payment, please share the screenshot here "
            "or send it to our reception.\n"
            f"{CONTACT_PHONE_LINES}"
        )

    # ── Outdoor sports & activities ──
    if any(w in t for w in ("activit", "sport", "outdoor", "play", "game", "cricket",
                             "badminton", "football", "basketball", "archery", "cycling",
                             "indoor", "table tennis", "foosball", "carrom", "chess")):
        return (
            "🎯 *Kapila River Front – Activities*\n\n"
            "🏏 *Outdoor Sports:*\n"
            "• Netted Cricket\n"
            "• Badminton\n"
            "• Football\n"
            "• Basketball\n"
            "• Archery\n"
            "• Cycling\n\n"
            "🎲 *Indoor Games:*\n"
            "• Table Tennis\n"
            "• Foosball\n"
            "• Carrom\n"
            "• Chess\n"
            "• Puzzle Games\n\n"
            "🏊 *Recreation:*\n"
            "• Swimming Pool\n"
            "• Music System\n\n"
            "✅ All activities are *included* with your stay!\n\n"
            "Type *pool* for swimming pool details."
        )

    # ── Amenities ──
    if any(w in t for w in ("amenit", "facilit", "include", "provide", "offer")):
        return (
            "🌿 *Kapila River Front – Amenities*\n\n"
            "🏠 *In-Room:*\n"
            "• Air Conditioning\n"
            "• Television\n"
            "• Hot water kettle\n"
            "• Attached modern washroom\n"
            "• Private balcony sit-out\n\n"
            "🏟 *On-Site:*\n"
            "• Swimming Pool\n"
            "• Netted Cricket, Badminton, Football, Basketball\n"
            "• Archery & Cycling\n"
            "• Table Tennis, Foosball, Carrom, Chess\n"
            "• Music System\n\n"
            "🍽 *Included:*\n"
            "• All meals (welcome drinks, lunch, high tea, dinner & breakfast)\n"
            "• Peaceful riverside setting\n"
            "• Heritage-style architecture\n\n"
            "Type *activities* for the full list or *price* for rates."
        )

    # ── Swimming pool ──
    if any(w in t for w in ("pool", "swim", "swimming")):
        return (
            "🏊 *Swimming Pool*\n\n"
            "Yes! We have a swimming pool on-site. 💦\n\n"
            "• Accessible to all in-house guests\n"
            "• Included with your stay – no extra charge\n"
            "• Perfect for a refreshing dip after outdoor sports!\n\n"
            "Type *activities* to see all the fun things to do."
        )

    # ── Location / directions ──
    if any(w in t for w in ("location", "address", "direction", "where",
                             "reach", "map", "route", "mysore", "mysuru")):
        return (
            "📍 *How to Reach Kapila River Front*\n\n"
            "Kapila River Front is a luxury farm villa "
            "on the riverside near Mysore.\n\n"
            "📌 For exact location & Google Maps pin, "
            "please contact us:\n"
            f"{CONTACT_PHONE_LINES}\n\n"
            "We'll share the directions right away! 🗺"
        )

    # ── Food / dining ──
    if any(w in t for w in ("food", "meal", "breakfast", "lunch", "dinner",
                             "dining", "eat", "restaurant", "tea", "drink")):
        return (
            "🍽 *Dining at Kapila River Front*\n\n"
            "All meals are *included* with your stay:\n\n"
            "☕ Welcome drinks on arrival\n"
            "🍛 Lunch\n"
            "🍵 High tea / evening snacks\n"
            "🍽 Dinner\n"
            "🥞 Breakfast (next morning)\n\n"
            "For special dietary needs or meal preferences, "
            "please inform us in advance:\n"
            f"{CONTACT_PHONE_LINES}"
        )

    # ── Valentine's Day ──
    if any(w in t for w in ("valentine", "14 feb", "14th feb")):
        return (
            "💝 *Valentine's Day Special – 14th February*\n\n"
            "🛏 *₹14,000 per night*\n"
            "• Double occupancy\n"
            "• All meals included\n\n"
            "A perfect romantic riverside getaway! 🌹\n\n"
            "Type *book* to reserve or *price* for the full rate card."
        )

    # ── Dasara ──
    if any(w in t for w in ("dasara", "dussehra", "october fest")):
        return (
            "🎆 *Dasara Festival Rates*\n\n"
            "During the *10-day Dasara festival*:\n"
            "• *₹13,000 per night* (all days)\n"
            "• All meals included\n\n"
            "Type *book* to reserve or *price* for the full rate card."
        )

    # ── Thank you / bye ──
    if any(w in t for w in ("thank", "thanks", "bye", "goodbye", "see you")):
        return (
            "Thank you for choosing *Kapila River Front*! 🙏🌿\n\n"
            "We look forward to hosting you.\n"
            "Feel free to message anytime!\n\n"
            "Have a wonderful day! 😊"
        )

    # ── Menu / help ──
    if any(w in t for w in ("menu", "help", "option", "what can")):
        return (
            "📋 *Here's everything I can help with:*\n\n"
            "📸 *gallery* – Photos (indoor / outdoor / activities)\n"
            "🛏 *room* – Room details & features\n"
            "💰 *price* – 2026 Rate card\n"
            "🎆 *newyear* – NYE special package\n"
            "💝 *valentine* – Valentine's Day offer\n"
            "📅 *book* – Booking enquiry\n"
            "🐾 *pet* – Pet policy & charges\n"
            "🎯 *activities* – Sports & games\n"
            "🌿 *amenities* – Facilities overview\n"
            "🏊 *pool* – Swimming pool info\n"
            "🍽 *food* – Dining & meals\n"
            "📍 *location* – How to reach us\n"
            "❌ *cancel* – Cancellation policy\n"
            "🏦 *payment* – Bank details\n"
            "📞 *call* / *kavitha* – Tap-to-call numbers\n"
            "👨‍💼 *reception* – Reception desk\n\n"
            "Just type any keyword! 😊"
        )

    # ── Default fallback ──
    return (
        "Thank you for reaching out to "
        "*Kapila River Front*! 🌿\n\n"
        "I can help you with:\n"
        "📸 *gallery* – Photo gallery\n"
        "🛏 *room* – Room info\n"
        "💰 *price* – 2026 Rates\n"
        "📅 *book* – Booking enquiry\n"
        "🐾 *pet* – Pet policy\n"
        "🎯 *activities* – Things to do\n"
        "❌ *cancel* – Cancellation policy\n"
        "📋 *menu* – See all options\n\n"
        "Or type your question and our team "
        "will get back to you! 🙏"
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


def _normalize_whatsapp_digits(raw: str) -> str:
    return "".join(c for c in (raw or "").strip() if c.isdigit())


def format_desk_enquiry_alert(payload: dict, source_label: str) -> str:
    """Plain-text summary for staff (no user-controlled * that could break formatting)."""
    def _plain(s: object) -> str:
        return str(s or "").replace("*", "").replace("_", "").strip()

    name = _plain(payload.get("name"))
    # Make the phone easier to dial: show as +<countrycode><number> when possible.
    raw_phone = _plain(payload.get("phone"))
    phone_digits = _normalize_whatsapp_digits(raw_phone)
    if len(phone_digits) == 10:
        phone = "+91" + phone_digits
    elif len(phone_digits) >= 11:
        phone = "+" + phone_digits
    else:
        phone = raw_phone
    visit_purpose = _plain(payload.get("visit_purpose"))
    fnq = payload.get("fnq", "")
    rooms = payload.get("rooms", "")
    occupancy = payload.get("occupancy", "")
    tent_extra_packs = payload.get("tent_extra_packs", "")
    walkin_available = payload.get("walkin_available", "")

    cin = _plain(payload.get("check_in"))
    cout = _plain(payload.get("check_out"))
    packs = payload.get("packs", "")

    lines: list[str] = []
    lines.append("🔔 *New enquiry — please call guest*")
    lines.append("")
    lines.append(f"📋 *Source:* {source_label}")
    lines.append("")
    lines.append(f"👤 *Name:* {name}")
    lines.append(f"📞 *Phone:* {phone}")

    if visit_purpose:
        lines.append(f"📝 *Visit purpose:* {visit_purpose}")

    # Booking enquiry fields
    if cin:
        lines.append(f"📅 *Check-in:* {cin}")
    if cout:
        lines.append(f"📅 *Check-out:* {cout}")
    if packs not in ("", None):
        lines.append(f"👥 *Total guests:* {packs}")

    # Property visit fields (your desk needs these)
    if fnq not in ("", None):
        lines.append(f"🔢 *FNQ:* {fnq}")
    if rooms not in ("", None) and rooms != "":
        lines.append(f"🏠 *Number of rooms:* {rooms}")
    if occupancy not in ("", None) and occupancy != "":
        lines.append(f"👥 *Total occupancy:* {occupancy} members")
    if tent_extra_packs not in ("", None) and tent_extra_packs != "":
        lines.append(f"⛺ *Tent extra:* {tent_extra_packs} packs")
    if walkin_available not in ("", None):
        lines.append(
            f"🚶 *Walk-in:* {'Available' if bool(walkin_available) else 'Not available'}"
        )

    return "\n".join(lines) + "\n"


async def notify_desk_staff(payload: dict, source_label: str) -> None:
    """
    Send the same enquiry details saved to enquiries.jsonl to desk WhatsApp number(s).
    Set DESK_NOTIFY_WHATSAPP in the environment (comma-separated).
    """
    if not DESK_NOTIFY_WHATSAPP:
        return
    recipients: list[str] = []
    for part in DESK_NOTIFY_WHATSAPP.split(","):
        n = _normalize_whatsapp_digits(part)
        if len(n) >= 10:
            recipients.append(n)
    if not recipients:
        logger.warning("desk_notify     | DESK_NOTIFY_WHATSAPP has no valid numbers")
        return
    body = format_desk_enquiry_alert(payload, source_label)
    for to in recipients:
        await send_message(to, body)


def parse_chat_date(text: str) -> date | None:
    """Parse YYYY-MM-DD or DD/MM/YYYY (or DD-MM-YYYY) from chat text."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def validate_enquiry_phone(text: str) -> str | None:
    """Same rules as the web form: 8–20 chars, must contain a digit."""
    p = (text or "").strip()
    if len(p) < 8 or len(p) > 20:
        return None
    if not any(c.isdigit() for c in p):
        return None
    return p


def parse_packs_int(text: str, min_val: int = 1, max_val: int = 17) -> int | None:
    t = (text or "").strip()
    try:
        n = int(t)
        if min_val <= n <= max_val:
            return n
    except ValueError:
        pass
    m = re.search(r"\b(\d{1,3})\b", t)
    if m:
        n = int(m.group(1))
        if min_val <= n <= max_val:
            return n
    return None


async def send_contact_card(to: str) -> None:
    """Send a vCard-style contact (WhatsApp lets users tap numbers to call)."""
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "contacts",
        "contacts": [
            {
                "name": {
                    "formatted_name": f"{CONTACT_NAME} – Kapila River Front",
                    "first_name": CONTACT_NAME,
                    "last_name": "Reservations",
                },
                "org": {
                    "company": "Kapila River Front",
                    "title": "Reservations",
                },
                "phones": [
                    {
                        "phone": PHONE_KAVITHA,
                        "type": "CELL",
                        "wa_id": "919108138510",
                    },
                    {"phone": PHONE_KAVITHA_ALT, "type": "WORK"},
                    {"phone": PHONE_RECEPTION_24_7, "type": "MAIN"},
                ],
            }
        ],
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                GRAPH_API_URL, headers=headers, json=payload
            )
            logger.info("send_contact  | to=%s | status=%s", to, response.status_code)
            logger.info("send_contact  | response=%s", response.text)
        except httpx.RequestError as exc:
            logger.error("send_contact  | request failed: %s", exc)


async def send_kavitha_call_help(to: str) -> None:
    """Contact card + text with tappable E.164 numbers (opens phone dialer on tap)."""
    await send_contact_card(to)
    await send_message(
        to,
        "☎️ *Call Kavitha*\n\n"
        "Tap any *+91…* number in the contact card above or below — "
        "your phone will open a normal voice call.\n\n"
        f"{PHONE_KAVITHA}\n"
        f"{PHONE_KAVITHA_ALT}\n"
        f"{PHONE_RECEPTION_24_7}\n\n"
        "_Primary · Alternate · 24/7 reception_",
    )


async def send_image(to: str, image_url: str, caption: str | None = None) -> None:
    """Send an image by public HTTPS URL (WhatsApp Cloud API)."""
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    image_obj: dict = {"link": image_url}
    if caption:
        image_obj["caption"] = caption
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image_obj,
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                GRAPH_API_URL, headers=headers, json=payload
            )
            logger.info("send_image    | to=%s | status=%s", to, response.status_code)
            logger.info("send_image    | response=%s", response.text)
        except httpx.RequestError as exc:
            logger.error("send_image    | request failed: %s", exc)


GALLERY_LABELS = {
    "indoor": "Indoor",
    "outdoor": "Outdoor",
    "activities": "Activities",
}


async def send_gallery_images(to: str, category: str) -> None:
    """Send all images from photos/<category>/ via public URLs."""
    if category not in GALLERY_LABELS:
        logger.warning("gallery       | unknown category: %s", category)
        return

    if not PUBLIC_BASE_URL:
        logger.error("gallery       | PUBLIC_BASE_URL / RENDER_EXTERNAL_URL not set")
        await send_message(
            to,
            "⚠️ Gallery links are not configured on the server.\n"
            f"Please contact us:\n{CONTACT_PHONE_LINES}"
        )
        return

    filenames = list_gallery_filenames(category)
    label = GALLERY_LABELS[category]

    if not filenames:
        logger.info("gallery       | no images in category=%s", category)
        await send_message(
            to,
            f"No photos in *{label}* yet.\n{CONTACT_PHONE_LINES}"
        )
        return

    logger.info("gallery       | sending %s images for %s", len(filenames), category)
    await send_message(
        to,
        f"📸 *{label}* – sending {len(filenames)} photo(s)...",
    )

    for i, name in enumerate(filenames, 1):
        url = public_photo_url(category, name)
        cap = f"Kapila – {label} ({i}/{len(filenames)})"
        await send_image(to, url, caption=cap)
        await asyncio.sleep(0.4)

    await send_gallery_menu(to)


# ──────────────────────────────────────────────
# Send interactive button menus via the Graph API
# ──────────────────────────────────────────────
async def _send_interactive(to: str, body_text: str, buttons: list[dict]) -> None:
    """Low-level helper to send any interactive button message (Meta max 3 reply buttons)."""
    if len(buttons) > WHATSAPP_MAX_REPLY_BUTTONS:
        logger.warning(
            "interactive   | truncating %s buttons to %s (Meta limit)",
            len(buttons),
            WHATSAPP_MAX_REPLY_BUTTONS,
        )
        buttons = buttons[:WHATSAPP_MAX_REPLY_BUTTONS]
    for b in buttons:
        title = (b.get("reply") or {}).get("title") or ""
        if len(title) > WHATSAPP_MAX_BUTTON_TITLE_LEN:
            logger.warning(
                "interactive   | button title too long (%s): %s…",
                len(title),
                title[:24],
            )
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": buttons},
        },
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                GRAPH_API_URL, headers=headers, json=payload
            )
            logger.info("send_buttons  | to=%s | status=%s", to, response.status_code)
            logger.info("send_buttons  | response=%s", response.text)
        except httpx.RequestError as exc:
            logger.error("send_buttons  | request failed: %s", exc)


async def send_button_message(to: str) -> None:
    """Home menu: About / Booking / More."""
    await _send_interactive(
        to,
        "Welcome to *Kapila River Front*! 🌿🏨\n"
        "A Luxury Farm Villa on the Riverside\n\n"
        "Choose what you need:",
        [
            {"type": "reply", "reply": {"id": "about", "title": "About ℹ️"}},
            {"type": "reply", "reply": {"id": "booking", "title": "Booking 📅"}},
            {"type": "reply", "reply": {"id": "more", "title": "More"}},
        ],
    )


async def send_booking_menu(to: str) -> None:
    """Booking sub-menu: Room Booking / Venue Booking / Day Out."""
    await _send_interactive(
        to,
        "📅 *Booking options*\nWhat are you looking for?",
        [
            {"type": "reply", "reply": {"id": "room_booking", "title": "Room Booking 🛏"}},
            {"type": "reply", "reply": {"id": "venue_booking", "title": "Venue Booking 💼"}},
            {"type": "reply", "reply": {"id": "day_out", "title": "Day Out 🌿"}},
        ],
    )


async def send_more_options(to: str) -> None:
    """More menu: FAQ / Gallery / Get a callback."""
    await _send_interactive(
        to,
        "More options 🌿",
        [
            {"type": "reply", "reply": {"id": "faq", "title": "FAQ ❓"}},
            {"type": "reply", "reply": {"id": "gallery", "title": "Gallery 📸"}},
            {"type": "reply", "reply": {"id": "callback_start", "title": "Get callback 📞"}},
        ],
    )


async def send_gallery_menu(to: str) -> None:
    """Sub-menu: indoor / outdoor / activities photos."""
    await _send_interactive(
        to,
        "📸 *Photo Gallery*\nChoose a category:",
        [
            {"type": "reply", "reply": {"id": "gal_indoor", "title": "Indoor 🏠"}},
            {"type": "reply", "reply": {"id": "gal_outdoor", "title": "Outdoor 🌿"}},
            {"type": "reply", "reply": {"id": "gal_activities", "title": "Activities 🎯"}},
        ],
    )


async def send_pet_policies_menu(to: str) -> None:
    """Third menu – pet policy, cancellation, payment."""
    await _send_interactive(
        to,
        "🐾 *Pet & Policies*",
        [
            {"type": "reply", "reply": {"id": "pet", "title": "Pet Policy 🐾"}},
            {"type": "reply", "reply": {"id": "cancel", "title": "Cancellation ❌"}},
            {"type": "reply", "reply": {"id": "payment", "title": "Payment Info 🏦"}},
        ],
    )


async def send_policies_menu(to: str) -> None:
    """Third menu – cancellation, payment, reception."""
    await _send_interactive(
        to,
        "📄 *Booking & Policies*",
        [
            {"type": "reply", "reply": {"id": "cancel", "title": "Cancellation ❌"}},
            {"type": "reply", "reply": {"id": "payment", "title": "Payment Info 🏦"}},
            {"type": "reply", "reply": {"id": "reception", "title": "Reception 👨‍💼"}},
        ],
    )


async def handle_callback_flow(sender: str, text: str) -> bool:
    """
    Multi-step callback: name → phone → check-in → check-out → guests (packs).
    Returns True if this message was handled in the callback flow.
    """
    step = sessions.callback_step_for(sender)
    if step == sessions.CALLBACK_IDLE:
        return False

    tl = text.lower().strip()
    if tl in ("cancel", "stop", "exit", "quit"):
        sessions.callback_abort(sender)
        await send_message(
            sender,
            "Callback request cancelled. Tap *Room Booking* on the menu to start again "
            "if you need help.",
        )
        await send_button_message(sender)
        return True

    if step == sessions.CALLBACK_NAME:
        name = text.strip()
        if len(name) < 2 or len(name) > 120:
            await send_message(
                sender,
                "Please send your *full name* (2–120 characters), or type *cancel* to stop.",
            )
            return True
        sessions.callback_after_name(sender, name)
        await send_message(
            sender,
            "Thanks *"
            + name.replace("*", "")
            + "*! 📞\n\n"
            "Now share your *phone number* (with country code if outside India).\n\n"
            "Example: `+919876543210` or `9876543210`",
        )
        return True

    if step == sessions.CALLBACK_PHONE:
        phone = validate_enquiry_phone(text)
        if not phone:
            await send_message(
                sender,
                "Please send a valid phone number (8–20 characters, include digits). "
                "Or type *cancel*.",
            )
            return True
        sessions.callback_after_phone(sender, phone)
        # For property-visit enquiries we finalize immediately after phone.
        if sessions.callback_mode_for(sender) == sessions.CALLBACK_MODE_PROPERTY_VISIT:
            record = sessions.callback_make_property_visit_record(sender)
            if not record:
                await send_message(
                    sender,
                    "Could not create your request. Please tap *Room Booking* again.",
                )
                await send_button_message(sender)
                return True
            try:
                append_enquiry_record(record)
            except OSError as exc:
                logger.exception("property_visit | save failed: %s", exc)
                sessions.callback_clear_flow(sender)
                await send_message(
                    sender,
                    "We could not save your enquiry. Please try again or contact us.",
                )
                await send_button_message(sender)
                return True
            sessions.callback_clear_flow(sender)
            await notify_desk_staff(record, "Property visit (WhatsApp)")
            await send_message(
                sender,
                "✅ *Thank you!* We received your *property visit* request.\n\n"
                f"• *Name:* {record['name']}\n"
                f"• *Phone:* {record['phone']}\n\n"
                "Our team will contact you soon. 🙏",
            )
            await send_button_message(sender)
            return True

        # Booking enquiry: continue with dates.
        await send_message(
            sender,
            "📅 *Check-in date*\n\n"
            "Send the date in *YYYY-MM-DD* (e.g. `2026-04-15`) or *DD/MM/YYYY*.",
        )
        return True

    if step == sessions.CALLBACK_CHECKIN:
        d = parse_chat_date(text)
        if not d:
            await send_message(
                sender,
                "Please send a valid check-in date (e.g. `2026-04-15` or `15/04/2026`). "
                "Or *cancel*.",
            )
            return True
        sessions.callback_after_checkin(sender, d.isoformat())
        await send_message(
            sender,
            "📅 *Check-out date*\n\n"
            "Must be on or after check-in. Same format: `YYYY-MM-DD` or `DD/MM/YYYY`.",
        )
        return True

    if step == sessions.CALLBACK_CHECKOUT:
        d_out = parse_chat_date(text)
        if not d_out:
            await send_message(
                sender,
                "Please send a valid check-out date. Or *cancel*.",
            )
            return True
        checkin_iso = sessions.callback_get_checkin_iso(sender)
        try:
            d_in = date.fromisoformat(checkin_iso)
        except ValueError:
            sessions.callback_abort(sender)
            await send_message(sender, "Something went wrong. Please tap *Room Booking* again.")
            await send_button_message(sender)
            return True
        if d_out < d_in:
            await send_message(
                sender,
                "Check-out must be *on or after* check-in "
                f"({d_in.isoformat()}). Please send a valid date.",
            )
            return True
        sessions.callback_after_checkout(sender, d_out.isoformat())
        await send_message(
            sender,
            "👥 *How many guests?*\n\n"
            + (
                "Send the *total number of people* (50–250)."
                if sessions.callback_mode_for(sender) == sessions.CALLBACK_MODE_VENUE
                else "Send the *total number of people* (min 10)."
                if sessions.callback_mode_for(sender) == sessions.CALLBACK_MODE_DAYOUT
                else "Send the *total number of people* (1–17)."
            ),
        )
        return True

    if step == sessions.CALLBACK_PACKS:
        mode = sessions.callback_mode_for(sender)
        if mode == sessions.CALLBACK_MODE_VENUE:
            min_g, max_g, label = 50, 250, "50\u2013250"
        elif mode == sessions.CALLBACK_MODE_DAYOUT:
            min_g, max_g, label = 10, 500, "at least 10"
        else:
            min_g, max_g, label = 1, 17, "1\u201317"

        packs = parse_packs_int(text, min_val=min_g, max_val=max_g)
        if packs is None:
            await send_message(
                sender,
                f"Please send a number (*{label}* guests). Or *cancel*.",
            )
            return True
        record = sessions.callback_make_record(sender, packs)
        if not record:
            await send_message(sender, "Could not save your request. Please try *Booking* again.")
            await send_button_message(sender)
            return True
        try:
            append_enquiry_record(record)
        except OSError as exc:
            logger.exception("callback       | save failed: %s", exc)
            await send_message(
                sender,
                "We could not save your enquiry. Please try sending the guest count again, "
                "or type *cancel* and start over.",
            )
            return True
        sessions.callback_clear_flow(sender)
        _src = {
            sessions.CALLBACK_MODE_ROOM: "Room booking (WhatsApp)",
            sessions.CALLBACK_MODE_VENUE: "Venue booking (WhatsApp)",
            sessions.CALLBACK_MODE_DAYOUT: "Day out (WhatsApp)",
        }
        await notify_desk_staff(record, _src.get(mode, "WhatsApp callback"))
        await send_message(
            sender,
            "✅ *Thank you!* We’ve received your booking enquiry:\n\n"
            f"• *Name:* {record['name']}\n"
            f"• *Phone:* {record['phone']}\n"
            f"• *Check-in:* {record['check_in']}\n"
            f"• *Check-out:* {record['check_out']}\n"
            f"• *Guests:* {record['packs']}\n\n"
            "Our team will contact you soon. 🙏",
        )
        await send_button_message(sender)
        return True

    return False


# ──────────────────────────────────────────────
# Handle interactive button clicks
# ──────────────────────────────────────────────
async def handle_button_click(sender: str, button_id: str) -> None:
    """Route logic based on the button ID the user tapped."""
    logger.info("button_click  | from=%s | button_id=%s", sender, button_id)

    _CALLBACK_STARTING_BUTTONS = {
        "room_booking", "venue_booking", "day_out", "visit", "callback_start",
    }
    if (
        button_id not in _CALLBACK_STARTING_BUTTONS
        and sessions.callback_step_for(sender) != sessions.CALLBACK_IDLE
    ):
        sessions.callback_abort(sender)

    # ── Home menu buttons ──
    if button_id == "about":
        await send_message(
            sender,
            "ℹ️ *About Kapila River Front*\n\n"
            "A Luxury Farm Villa on the Riverside near Mysore.\n\n"
            "🏠 *5 Heritage Rooms* (max 15 guests)\n"
            "🍽 *All meals included* (welcome drinks → breakfast)\n"
            "🏊 Swimming pool, cricket, badminton, archery & more\n"
            "🐾 Pets welcome (charges apply)\n"
            "🌿 Peaceful riverside setting\n\n"
            f"📞 Contact: {PHONE_KAVITHA}\n\n"
            "Tap *Booking* to reserve your stay!",
        )
        await send_button_message(sender)

    elif button_id == "booking":
        await send_booking_menu(sender)

    elif button_id == "more":
        await send_more_options(sender)

    # ── Booking sub-menu buttons ──
    elif button_id == "room_booking":
        sessions.callback_begin(sender, sessions.CALLBACK_MODE_ROOM)
        await send_message(
            sender,
            "🛏 *Room booking enquiry*\n"
            "_(max 17 guests)_\n\n"
            "Please reply with your *full name*.\n\n"
            "Type *cancel* any time to stop.",
        )

    elif button_id == "venue_booking":
        sessions.callback_begin(sender, sessions.CALLBACK_MODE_VENUE)
        await send_message(
            sender,
            "💼 *Venue booking*\n"
            "_(50–250 guests)_\n\n"
            "Please reply with your *full name*.\n\n"
            "Type *cancel* any time to stop.",
        )

    elif button_id == "day_out":
        sessions.callback_begin(sender, sessions.CALLBACK_MODE_DAYOUT)
        await send_message(
            sender,
            "🌿 *Day Out booking*\n"
            "_(min 10 people)_\n\n"
            "Please reply with your *full name*.\n\n"
            "Type *cancel* any time to stop.",
        )

    # ── More sub-menu buttons ──
    elif button_id == "faq":
        await send_message(
            sender,
            "❓ *FAQ — Kapila River Front*\n\n"
            "💰 *Rates:* From ₹10,000/night (all meals included)\n"
            "🕐 *Check-in:* 1 PM · *Check-out:* 11 AM\n"
            "🐾 *Pets:* Allowed (₹2,000/pet)\n"
            "❌ *Cancellation:* Free 15+ days before check-in\n"
            "🏦 *Payment:* 100% advance (bank transfer)\n\n"
            "Type *price*, *pet*, *cancel*, or *payment* for full details.",
        )
        await send_more_options(sender)

    elif button_id == "gallery":
        await send_gallery_menu(sender)

    elif button_id == "callback_start":
        sessions.callback_begin(sender, sessions.CALLBACK_MODE_ROOM)
        await send_message(
            sender,
            "📞 *Get a callback*\n\n"
            "Please reply with your *full name*.\n\n"
            "Type *cancel* any time to stop.",
        )

    # ── Gallery sub-menu buttons ──
    elif button_id == "gal_indoor":
        await send_gallery_images(sender, "indoor")

    elif button_id == "gal_outdoor":
        await send_gallery_images(sender, "outdoor")

    elif button_id == "gal_activities":
        await send_gallery_images(sender, "activities")

    # ── Legacy buttons (old cached menus users may still see) ──
    elif button_id == "contact_kavitha":
        await send_kavitha_call_help(sender)
        await send_button_message(sender)

    elif button_id == "price":
        await send_message(sender, generate_reply("price"))
        await send_button_message(sender)

    elif button_id == "room":
        await send_message(sender, generate_reply("room"))
        await send_booking_menu(sender)

    elif button_id == "activities":
        await send_message(sender, generate_reply("activities"))
        await send_booking_menu(sender)

    elif button_id == "pet":
        await send_message(sender, generate_reply("pet"))
        await send_more_options(sender)

    elif button_id == "cancel":
        await send_message(sender, generate_reply("cancel"))
        await send_more_options(sender)

    elif button_id == "payment":
        await send_message(sender, generate_reply("payment"))
        await send_more_options(sender)

    elif button_id == "pet_policies":
        await send_pet_policies_menu(sender)

    elif button_id == "visit":
        sessions.callback_begin(sender, sessions.CALLBACK_MODE_PROPERTY_VISIT)
        await send_message(
            sender,
            "🏡 *Property visit enquiry*\n\n"
            "Please reply with your *name*.\n\n"
            "Type *cancel* any time to stop.",
        )

    elif button_id == "policies":
        await send_policies_menu(sender)

    elif button_id == "reception":
        await send_message(
            sender,
            "👨‍💼 *Kapila River Front – Reception*\n\n"
            f"{CONTACT_PHONE_LINES}\n\n"
            "Tap any number to call. "
            f"*{CONTACT_NAME}* and our team are happy to help 24/7. 🙏",
        )
        await send_button_message(sender)

    else:
        await send_message(sender, generate_reply(""))
        await send_button_message(sender)


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
async def _dispatch_incoming_message(sender: str, msg: dict) -> None:
    """Route one WhatsApp message for a verified sender (wa_id)."""
    msg_type = msg.get("type") or "unknown"

    if msg_type == "interactive":
        button_id = (
            msg.get("interactive", {})
            .get("button_reply", {})
            .get("id", "")
        )
        sessions.touch(sender, f"btn:{button_id}")
        await handle_button_click(sender, button_id)

    elif msg_type == "text":
        text = (msg.get("text") or {}).get("body") or ""
        logger.info("webhook       | from=%s | text=%s", sender, text)

        if await handle_callback_flow(sender, text):
            sessions.touch(sender, "text:callback_flow")
            return

        greetings = (
            "hi", "hello", "hey", "hii", "helo",
            "good morning", "good afternoon", "good evening",
        )
        tl = text.lower()

        if text.lower().strip() in greetings:
            sessions.touch(sender, "text:greeting")
            await send_button_message(sender)
        elif any(
            k in tl
            for k in ("gallery", "photo", "photos", "picture", "pictures")
        ):
            sessions.touch(sender, "text:gallery")
            logger.info("webhook       | gallery keyword from text")
            await send_gallery_menu(sender)
        elif any(w in tl for w in ("menu", "help", "option")):
            sessions.touch(sender, "text:menu")
            await send_message(sender, generate_reply("menu"))
            await send_button_message(sender)
        elif tl.strip() in ("more", "more options"):
            sessions.touch(sender, "text:more")
            await send_more_options(sender)
        elif any(
            tl.strip() == w
            for w in ("call", "kavitha", "contact", "phone", "call kavitha")
        ) or "call kavitha" in tl:
            sessions.touch(sender, "text:call")
            await send_kavitha_call_help(sender)
            await send_button_message(sender)
        elif re.search(
            r"(property\s*visit|property\s*view|property\s*visite|visit\s*property|proe?r(?:y)?\s*vis|proery\s*vis|proe?r.*visite)",
            tl,
            re.I,
        ):
            sessions.touch(sender, "text:property_visit_start")
            sessions.callback_begin(
                sender, sessions.CALLBACK_MODE_PROPERTY_VISIT
            )
            await send_message(
                sender,
                "📞 *Property visit request*\n\n"
                "Please reply with your *full name* (as you'd like us to use).\n\n"
                "Type *cancel* any time to stop.",
            )
        elif re.search(r"\b(callback|call back)\b", tl, re.I):
            sessions.touch(sender, "text:callback_start")
            sessions.callback_begin(sender, sessions.CALLBACK_MODE_BOOKING)
            await send_message(
                sender,
                "📞 *Request a callback*\n\n"
                "Please reply with your *full name* (as you'd like us to use).\n\n"
                "Type *cancel* any time to stop.",
            )
        else:
            sessions.touch(sender, "text:reply")
            reply = generate_reply(text)
            await send_message(sender, reply)

    else:
        sessions.touch(sender, f"type:{msg_type}")
        logger.info("webhook       | from=%s | unhandled type=%s", sender, msg_type)
        await send_message(sender, generate_reply(""))
        await send_button_message(sender)

    logger.info("webhook       | done wa_id=%s", sender)


async def process_webhook_payload(body: dict) -> None:
    """Handle WhatsApp webhook JSON (runs in background after 200 OK)."""
    try:
        if not isinstance(body, dict):
            logger.error("webhook       | payload is not an object")
            return

        entry = body.get("entry")
        if not isinstance(entry, list):
            return

        for e in entry:
            changes = e.get("changes") or []
            if not isinstance(changes, list):
                continue
            for change in changes:
                value = change.get("value") or {}

                for st in value.get("statuses") or []:
                    logger.info(
                        "webhook       | delivery status id=%s status=%s",
                        st.get("id"),
                        st.get("status"),
                    )

                messages = value.get("messages") or []
                if not isinstance(messages, list):
                    continue

                for msg in messages:
                    try:
                        sender = msg.get("from")
                        if not sender or not isinstance(sender, str):
                            logger.warning(
                                "webhook       | skip message without from id=%s",
                                msg.get("id"),
                            )
                            continue
                        await _dispatch_incoming_message(sender, msg)
                    except Exception as exc:
                        logger.exception(
                            "webhook       | single-message error wa_id=%s: %s",
                            msg.get("from"),
                            exc,
                        )

    except Exception as exc:
        logger.exception("webhook       | error processing payload: %s", exc)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    WhatsApp Cloud API webhook. Verifies Meta signature when APP_SECRET is set.
    Returns 200 quickly; work continues in a background task.
    """
    raw_body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256") or request.headers.get(
        "x-hub-signature-256"
    )

    if not webhook_auth.verify_webhook_signature(raw_body, sig):
        logger.warning("webhook       | rejected: invalid or missing signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("webhook       | invalid JSON: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    if body.get("object") != "whatsapp_business_account":
        logger.info(
            "webhook       | ignored object=%s", body.get("object"),
        )
        return {"status": "ignored"}

    logger.debug("webhook       | incoming payload: %s", body)
    asyncio.create_task(process_webhook_payload(body))
    return {"status": "ok"}
