import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import telephony


def _with_env(**env):
    class _Ctx:
        def __enter__(self):
            self.old = {k: os.environ.get(k) for k in env}
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        def __exit__(self, *exc):
            for k, v in self.old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    return _Ctx()


def test_signature_algorithm_matches_twilios_documented_scheme():
    """Twilio signs: URL + each POST param as name+value in sorted-name order,
    HMAC-SHA1 with the auth token, base64 (checked against the algorithm in
    Twilio's own SDK validator). Computed independently here."""
    import base64
    import hashlib
    import hmac

    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    form = {"To": "+18005551212", "From": "+14158675310", "CallSid": "CA1234567890ABCDE", "Digits": "1234"}
    data = url + "CallSidCA1234567890ABCDE" + "Digits1234" + "From+14158675310" + "To+18005551212"
    expected = base64.b64encode(hmac.new(b"12345", data.encode(), hashlib.sha1).digest()).decode()
    assert telephony.twilio_signature("12345", url, form) == expected


def test_signature_accepted_with_or_without_explicit_default_port():
    with _with_env(TWILIO_AUTH_TOKEN="tok"):
        form = {"CallSid": "CA1"}
        sig = telephony.twilio_signature("tok", "https://h.example:443/twilio/voice", form)
        assert telephony.request_is_from_twilio("https://h.example/twilio/voice", form, sig)
        sig2 = telephony.twilio_signature("tok", "https://h.example/twilio/voice", form)
        assert telephony.request_is_from_twilio("https://h.example:443/twilio/voice", form, sig2)


def test_signature_accepted_when_console_url_was_typed_with_the_other_scheme():
    with _with_env(TWILIO_AUTH_TOKEN="tok"):
        form = {"CallSid": "CA1"}
        sig = telephony.twilio_signature("tok", "http://h.example/twilio/voice", form)  # pasted http://
        assert telephony.request_is_from_twilio("https://h.example/twilio/voice", form, sig)
        assert not telephony.request_is_from_twilio("https://other.example/twilio/voice", form, sig)


def test_request_validation_accepts_genuine_and_rejects_forged():
    with _with_env(TWILIO_AUTH_TOKEN="tok"):
        url, form = "https://x.example/twilio/voice", {"CallSid": "CA1", "From": "+911"}
        good = telephony.twilio_signature("tok", url, form)
        assert telephony.request_is_from_twilio(url, form, good)
        assert not telephony.request_is_from_twilio(url, {**form, "From": "+912"}, good)  # params tampered
        assert not telephony.request_is_from_twilio("https://evil.example/twilio/voice", form, good)
        assert not telephony.request_is_from_twilio(url, form, None)
        assert not telephony.request_is_from_twilio(url, form, "bogus")


def test_without_an_auth_token_nothing_validates():
    with _with_env(TWILIO_AUTH_TOKEN=None):
        try:
            telephony.request_is_from_twilio("https://x", {}, "sig")
        except telephony.TelephonyNotConfigured:
            pass
        else:
            raise AssertionError("expected TelephonyNotConfigured")
        assert not telephony.stream_token_valid("CA1", "anything")


def test_stream_token_is_bound_to_its_call():
    with _with_env(TWILIO_AUTH_TOKEN="tok"):
        t = telephony.stream_token("CA111")
        assert telephony.stream_token_valid("CA111", t)
        assert not telephony.stream_token_valid("CA222", t)  # token minted for another call
        assert not telephony.stream_token_valid("CA111", None)
        assert not telephony.stream_token_valid(None, t)
    with _with_env(TWILIO_AUTH_TOKEN="other"):
        assert not telephony.stream_token_valid("CA111", t)  # minted under a different secret


def test_twiml_is_well_formed_and_round_trips_awkward_values():
    import xml.etree.ElementTree as ET

    xml = telephony.twiml_connect_stream("wss://h/twilio/ws", {"auth": 'a"b<c&d', "from_number": "+91"})
    root = ET.fromstring(xml)  # raises if the escaping were wrong
    stream = root.find("Connect/Stream")
    assert stream.get("url") == "wss://h/twilio/ws"
    assert {p.get("name"): p.get("value") for p in stream.findall("Parameter")} == {
        "auth": 'a"b<c&d', "from_number": "+91",
    }


def test_signature_check_defaults_on_and_only_explicit_off_disables_it():
    for value, expected in [(None, True), ("", True), ("true", True), ("garbage", True),
                            ("false", False), ("FALSE", False), ("0", False), ("off", False), (" no ", False)]:
        with _with_env(TWILIO_VALIDATE_SIGNATURE=value):
            assert telephony.signature_check_enabled() is expected, value


def test_unsigned_request_needs_our_account_sid_and_an_allowed_number():
    env = dict(TWILIO_ACCOUNT_SID="ACmine", TWILIO_ALLOWED_TO="+919999999999")
    with _with_env(**env):
        ok = {"AccountSid": "ACmine", "To": "+919999999999", "From": "+17370000000"}
        assert telephony.unsigned_request_acceptable(ok)
        assert telephony.unsigned_request_acceptable({**ok, "To": "+1555", "From": "+919999999999"})  # inbound
        assert not telephony.unsigned_request_acceptable({**ok, "AccountSid": "ACother"})
        assert not telephony.unsigned_request_acceptable({**ok, "To": "+1555", "From": "+1556"})
        assert not telephony.unsigned_request_acceptable({})
    with _with_env(TWILIO_ACCOUNT_SID=None, TWILIO_ALLOWED_TO="+919999999999"):
        assert not telephony.unsigned_request_acceptable({"AccountSid": "", "To": "+919999999999"})
    with _with_env(TWILIO_ACCOUNT_SID="ACmine", TWILIO_ALLOWED_TO=None):
        assert not telephony.unsigned_request_acceptable({"AccountSid": "ACmine", "To": "+919999999999"})


def test_destination_allow_list():
    with _with_env(TWILIO_ALLOWED_TO="+919999999999, +14155550100"):
        assert telephony.destination_allowed("+919999999999")
        assert telephony.destination_allowed(" +14155550100 ")
        assert not telephony.destination_allowed("+919999999998")
        assert not telephony.destination_allowed("")
    with _with_env(TWILIO_ALLOWED_TO=None):
        assert not telephony.destination_allowed("+919999999999")  # empty list allows nothing


def test_public_base_url_prefers_override_then_proxy_headers():
    with _with_env(TWILIO_WEBHOOK_BASE_URL="https://pinned.example/"):
        assert telephony.public_base_url({"host": "ignored"}, "http://fallback") == "https://pinned.example"
    with _with_env(TWILIO_WEBHOOK_BASE_URL=None):
        headers = {"host": "internal:10000", "x-forwarded-host": "munshi.onrender.com", "x-forwarded-proto": "https"}
        assert telephony.public_base_url(headers, "http://fallback") == "https://munshi.onrender.com"
        assert telephony.public_base_url({}, "http://fallback/") == "http://fallback"


def test_signalwire_provider_uses_its_own_credentials_and_hosts():
    env = dict(
        TELEPHONY_PROVIDER="signalwire", SIGNALWIRE_SPACE="https://acme.signalwire.com/",
        SIGNALWIRE_PROJECT_ID="PJ1", SIGNALWIRE_API_TOKEN="PT1", SIGNALWIRE_SIGNING_KEY="SK1",
        SIGNALWIRE_FROM_NUMBER="+15550001111", TWILIO_ACCOUNT_SID="ACtwilio", TWILIO_AUTH_TOKEN="twilio-tok",
    )
    with _with_env(**env):
        assert telephony.provider() == "signalwire"
        assert telephony.account_sid() == "PJ1"
        assert telephony.api_token() == "PT1"
        assert telephony.from_number() == "+15550001111"
        assert telephony.rest_base() == "https://acme.signalwire.com/api/laml/2010-04-01"
        assert telephony.serializer_base_url() == "https://acme.signalwire.com/api/laml"
        # webhooks are verified (and stream tokens minted) with the Signing Key, not the API token
        url, form = "https://x.example/twilio/voice", {"CallSid": "CA1"}
        assert telephony.request_is_from_twilio(url, form, telephony.twilio_signature("SK1", url, form))
        assert not telephony.request_is_from_twilio(url, form, telephony.twilio_signature("PT1", url, form))
        assert telephony.stream_token_valid("CA1", telephony.stream_token("CA1"))


def test_signalwire_needs_space_and_signing_key():
    with _with_env(TELEPHONY_PROVIDER="signalwire", SIGNALWIRE_SPACE=None, SIGNALWIRE_SIGNING_KEY=None):
        for fn in (telephony.rest_base, telephony.serializer_base_url, lambda: telephony.stream_token("CA1")):
            try:
                fn()
            except telephony.TelephonyNotConfigured:
                pass
            else:
                raise AssertionError("expected TelephonyNotConfigured")


def test_twilio_stays_the_default_provider():
    with _with_env(TELEPHONY_PROVIDER=None, TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t", TWILIO_FROM_NUMBER="+1"):
        assert telephony.provider() == "twilio"
        assert telephony.rest_base() == "https://api.twilio.com/2010-04-01"
        assert telephony.serializer_base_url() is None
        assert telephony.account_sid() == "AC1" and telephony.api_token() == "t"
    with _with_env(TELEPHONY_PROVIDER="nonsense"):
        assert telephony.provider() == "twilio"


def test_signature_header_accepts_either_provider_header():
    assert telephony.signature_header({"x-signalwire-signature": "a", "x-twilio-signature": "b"}) == "a"
    assert telephony.signature_header({"x-twilio-signature": "b"}) == "b"
    assert telephony.signature_header({}) is None


def test_allowed_destinations_prefers_the_generic_variable():
    with _with_env(TELEPHONY_ALLOWED_TO="+911", TWILIO_ALLOWED_TO="+912"):
        assert telephony.allowed_destinations() == {"+911"}
    with _with_env(TELEPHONY_ALLOWED_TO=None, TWILIO_ALLOWED_TO="+912"):
        assert telephony.allowed_destinations() == {"+912"}


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
