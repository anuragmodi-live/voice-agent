"""Twilio telephony glue: TwiML, request/stream authentication, outbound calls.

The voice pipeline itself is unchanged -- a phone call just arrives over a
Twilio Media Streams WebSocket (8 kHz mu-law, converted by pipecat's
TwilioFrameSerializer) instead of WebRTC. This module holds only the parts that
are specific to Twilio and that must be right for the endpoints to be safe on
the public internet, where every call costs real money:

- /twilio/voice is a public webhook, so its X-Twilio-Signature is verified
  (Twilio's documented HMAC-SHA1 over the full URL + sorted POST params).
- /twilio/ws is a public WebSocket that Twilio does not let us put a query
  string on, so the signed webhook hands the stream a one-call token as a
  TwiML <Parameter>; the stream is refused unless that token matches its call.
- /twilio/call (outbound test call) is admin-key protected AND limited to an
  allow-list of destination numbers, so it can never be used to dial arbitrary
  (premium-rate, international) numbers.

Pure standard library + httpx, so it is importable (and testable) without
pipecat.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import quoteattr

import httpx

TWILIO_API = "https://api.twilio.com/2010-04-01"


class TelephonyNotConfigured(RuntimeError):
    pass


def _auth_token() -> str:
    token = os.getenv("TWILIO_AUTH_TOKEN") or ""
    if not token:
        raise TelephonyNotConfigured("TWILIO_AUTH_TOKEN is not set")
    return token


# ----------------------------------------------------------------- TwiML

def twiml_connect_stream(ws_url: str, parameters: dict[str, str] | None = None) -> str:
    params = "".join(
        f"<Parameter name={quoteattr(k)} value={quoteattr(str(v))}/>" for k, v in (parameters or {}).items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><Stream url={quoteattr(ws_url)}>{params}</Stream></Connect></Response>"
    )


# ----------------------------------------------------------------- authenticity

def twilio_signature(auth_token: str, url: str, form: dict[str, str]) -> str:
    """The value Twilio puts in X-Twilio-Signature for a POST webhook."""
    data = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def url_variants(url: str) -> list[str]:
    """Every spelling of `url` Twilio might have signed: with or without an
    explicit default port (:443/:80), and -- because the URL is signed exactly
    as typed into the Twilio console, and a pasted `http://` is easy to miss --
    with either scheme. Twilio's own SDK validator tries the port variants;
    the scheme variants cost nothing in security, since a forger still needs the
    auth token to produce any valid signature."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    variants = [url]
    for scheme, default_port in (("https", 443), ("http", 80)):
        for netloc in (host, f"{host}:{default_port}"):
            variants.append(urlunsplit(parts._replace(scheme=scheme, netloc=netloc)))
    return list(dict.fromkeys(variants))


def request_is_from_twilio(url: str, form: dict[str, str], signature: str | None) -> bool:
    if not signature:
        return False
    token = _auth_token()
    return any(
        hmac.compare_digest(twilio_signature(token, candidate, form), signature)
        for candidate in url_variants(url)
    )


def signature_check_enabled() -> bool:
    """On unless TWILIO_VALIDATE_SIGNATURE is explicitly false/0/no/off. Meant
    to be switched off only while testing: Twilio's console "Try out Voice ->
    Start a call" dialog sends its webhook request UNSIGNED, so with the check
    on it can never work. Set it in the Render dashboard, not in render.yaml
    (this repo is public and the safe default must stay on)."""
    return (os.getenv("TWILIO_VALIDATE_SIGNATURE") or "true").strip().lower() not in ("false", "0", "no", "off")


def unsigned_request_acceptable(form: dict[str, str]) -> bool:
    """The minimum still required of an UNSIGNED /twilio/voice request while the
    signature check is off: it must name our own Twilio AccountSid and involve
    one of the numbers in TWILIO_ALLOWED_TO (as caller or callee). This is far
    weaker than a signature -- the SID is not a secret -- but it keeps a casual
    stranger who finds the URL from starting billable sessions."""
    sid = os.getenv("TWILIO_ACCOUNT_SID") or ""
    allowed = allowed_destinations()
    return bool(sid) and form.get("AccountSid") == sid and (form.get("To") in allowed or form.get("From") in allowed)


def stream_token(call_sid: str) -> str:
    """One-call token embedded as a TwiML <Parameter> by the signed webhook."""
    return hmac.new(_auth_token().encode(), f"stream:{call_sid}".encode(), hashlib.sha256).hexdigest()


def stream_token_valid(call_sid: str | None, token: str | None) -> bool:
    if not call_sid or not token:
        return False
    try:
        return hmac.compare_digest(stream_token(call_sid), token)
    except TelephonyNotConfigured:
        return False


def public_base_url(headers, fallback_url: str) -> str:
    """Public https://host of this service, for URLs Twilio must call back.
    TWILIO_WEBHOOK_BASE_URL wins; otherwise it is rebuilt from the proxy
    headers Render sets (the app itself only sees plain http internally)."""
    override = (os.getenv("TWILIO_WEBHOOK_BASE_URL") or "").rstrip("/")
    if override:
        return override
    host = headers.get("x-forwarded-host") or headers.get("host")
    if not host:
        return fallback_url.rstrip("/")
    proto = headers.get("x-forwarded-proto", "https").split(",")[0].strip()
    return f"{proto}://{host}"


# ----------------------------------------------------------------- outbound test call

def allowed_destinations() -> set[str]:
    return {n.strip() for n in (os.getenv("TWILIO_ALLOWED_TO") or "").split(",") if n.strip()}


def destination_allowed(to: str) -> bool:
    return to.strip() in allowed_destinations()


async def place_call(to: str, voice_url: str) -> dict:
    """Ask Twilio to ring `to` from our number and, on answer, fetch TwiML from
    voice_url (our /twilio/voice). Returns Twilio's JSON for the new call."""
    sid = os.getenv("TWILIO_ACCOUNT_SID") or ""
    from_number = os.getenv("TWILIO_FROM_NUMBER") or ""
    if not (sid and from_number):
        raise TelephonyNotConfigured("TWILIO_ACCOUNT_SID / TWILIO_FROM_NUMBER are not set")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{TWILIO_API}/Accounts/{sid}/Calls.json",
            auth=(sid, _auth_token()),
            data={"To": to, "From": from_number, "Url": voice_url, "Method": "POST"},
        )
    resp.raise_for_status()
    return resp.json()
