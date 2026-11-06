"""Make the assistant phone someone. The app and the tunnel must already be running.

Run it from the project folder, with the number in international format:
    python -m scripts.place_call +923001234567

The call comes from TWILIO_PHONE_NUMBER. When the person answers, Twilio asks the app's webhook what to do,
the same route an incoming call uses, and the conversation then runs as usual.
"""
import re
import sys

from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client

from app.config import settings
from app.twilio import _public_origin

_INTERNATIONAL_NUMBER = re.compile(r"\+\d{8,15}")


def place_call(to_number, config):
    """Ask Twilio to place the call and return its Call SID. Raises ValueError for a bad number or missing settings."""
    if not _INTERNATIONAL_NUMBER.fullmatch(to_number):
        raise ValueError("Write the number in international format, with the plus sign, for example +923001234567.")
    origin = _public_origin(config.twilio_public_base_url)
    if not (config.twilio_account_sid and config.twilio_auth_token and config.twilio_phone_number and origin):
        raise ValueError("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER and TWILIO_PUBLIC_BASE_URL in .env.")
    http_client = TwilioHttpClient(timeout=config.twilio_api_timeout_seconds)
    client = Client(config.twilio_account_sid, config.twilio_auth_token, http_client=http_client)
    call = client.calls.create(to=to_number, from_=config.twilio_phone_number, url=f"{origin}/twilio/incoming", method="POST")
    return call.sid


def main(arguments):
    if len(arguments) != 1:
        print("Usage: python -m scripts.place_call +923001234567")
        return 1
    try:
        call_sid = place_call(arguments[0], settings)
    except ValueError as error:
        print(f"FAILED: {error}")
        return 1
    except TwilioRestException as error:
        print(f"FAILED: Twilio refused the call (error {error.code}): {error.msg}")
        return 1
    except Exception as error:
        print(f"FAILED: could not reach Twilio ({type(error).__name__}).")
        return 1
    print(f"OK: calling {arguments[0]} from {settings.twilio_phone_number}. Call SID: {call_sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
