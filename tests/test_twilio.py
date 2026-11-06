"""Twilio integration tests use local fakes and never contact Twilio or voice providers."""
import asyncio
import base64
import json
import xml.etree.ElementTree as ET
from dataclasses import replace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

import app.twilio as twilio_module
from app.config import settings
from app.twilio import TwilioCallTransport, build_twiml
from app.twilio_audio import TwilioAudioConverter, mulaw_to_pcm16, pcm16_to_mulaw
from tests.test_pipeline import NOW, Bench, ScriptedLLM

AUTH_TOKEN = "twilio-test-auth-token"
ORIGIN = "https://voice.example.test"
PHONE = "+15551234567"
ACCOUNT_SID = "AC_TEST_ACCOUNT_SID"


def run(coroutine):
    return asyncio.run(coroutine)


def twilio_settings():
    return replace(
        settings,
        twilio_account_sid=ACCOUNT_SID,
        twilio_auth_token=AUTH_TOKEN,
        twilio_phone_number=PHONE,
        twilio_public_base_url=ORIGIN,
    )


def make_app():
    api = FastAPI()
    api.include_router(twilio_module.router)
    api.state.llm = object()
    api.state.service = object()
    api.state.input_rail = object()
    api.state.output_rail = object()
    api.state.instructions = "booking instructions"
    return api


def _signed_form(parameters):
    url = ORIGIN + "/twilio/incoming"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(url, parameters)
    return {"x-twilio-signature": signature}


def test_twiml_uses_a_bidirectional_stream_and_a_fallback():
    xml = ET.fromstring(build_twiml("wss://voice.example.test/twilio/media-stream"))
    stream = xml.find("./Connect/Stream")
    assert stream is not None
    assert stream.get("url") == "wss://voice.example.test/twilio/media-stream"
    assert xml.find("./Say").text == settings.phone_fallback_message
    assert xml.find("./Hangup") is not None


def test_signed_incoming_call_returns_twiml(monkeypatch):
    monkeypatch.setattr(twilio_module, "settings", twilio_settings())
    parameters = {"AccountSid": ACCOUNT_SID, "CallSid": "CA111", "From": "+15557654321", "To": PHONE}
    with TestClient(make_app()) as client:
        response = client.post("/twilio/incoming", data=parameters, headers=_signed_form(parameters))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    xml = ET.fromstring(response.text)
    assert xml.find("./Connect/Stream").get("url") == "wss://voice.example.test/twilio/media-stream"


def test_incoming_call_rejects_bad_signature_and_wrong_number(monkeypatch):
    monkeypatch.setattr(twilio_module, "settings", twilio_settings())
    parameters = {"AccountSid": ACCOUNT_SID, "CallSid": "CA111", "To": PHONE}
    with TestClient(make_app()) as client:
        bad_signature = client.post("/twilio/incoming", data=parameters, headers={"x-twilio-signature": "bad"})
        wrong_number = {**parameters, "To": "+15550000000"}
        wrong_number_response = client.post(
            "/twilio/incoming", data=wrong_number, headers=_signed_form(wrong_number)
        )
    assert bad_signature.status_code == 403
    assert wrong_number_response.status_code == 403


def test_outgoing_call_must_come_from_our_number(monkeypatch):
    monkeypatch.setattr(twilio_module, "settings", twilio_settings())
    ours = {"AccountSid": ACCOUNT_SID, "CallSid": "CA111", "Direction": "outbound-api", "From": PHONE, "To": "+923001234567"}
    other = {**ours, "From": "+15550000000"}
    with TestClient(make_app()) as client:
        accepted = client.post("/twilio/incoming", data=ours, headers=_signed_form(ours))
        rejected = client.post("/twilio/incoming", data=other, headers=_signed_form(other))
    assert accepted.status_code == 200
    assert rejected.status_code == 403


def test_incoming_call_stays_unavailable_without_twilio_configuration(monkeypatch):
    unconfigured = replace(
        twilio_settings(),
        twilio_auth_token="",
        twilio_public_base_url="",
    )
    monkeypatch.setattr(twilio_module, "settings", unconfigured)
    with TestClient(make_app()) as client:
        response = client.post("/twilio/incoming", data={"CallSid": "CA111", "To": PHONE})
    assert response.status_code == 503


def test_websocket_signature_uses_the_documented_trailing_slash_fallback():
    url = "wss://voice.example.test/twilio/media-stream/"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(url, {})
    assert twilio_module._valid_signature(
        AUTH_TOKEN, signature, url.rstrip("/"), {}, websocket=True
    )
    assert not twilio_module._valid_signature(AUTH_TOKEN, "invalid", url.rstrip("/"), {}, websocket=True)


def test_mulaw_codec_and_resampling_preserve_audio_length():
    assert mulaw_to_pcm16(b"\xff\x7f") == b"\x00\x00\x00\x00"
    assert pcm16_to_mulaw(b"\x00\x00\x01") == b"\xff"
    assert mulaw_to_pcm16(b"") == b""
    assert pcm16_to_mulaw(b"\x01") == b""

    inbound = TwilioAudioConverter(stt_sample_rate=16000, tts_sample_rate=16000)
    decoded = inbound.decode_inbound(b"\xff" * 8000) + inbound.finish_inbound()
    assert len(decoded) == 16000 * 2

    tone = np.array(
        [int(8000 * np.sin(2 * np.pi * 440 * sample / 16000)) for sample in range(16000)],
        dtype="<i2",
    )
    outbound = TwilioAudioConverter(stt_sample_rate=16000, tts_sample_rate=16000)
    pieces = []
    for offset in range(0, len(tone), 640):
        pieces.append(outbound.encode_outgoing(tone[offset:offset + 640].tobytes()))
    pieces.append(outbound.finish_outgoing())
    encoded = b"".join(pieces)
    assert len(encoded) == 8000
    restored = np.frombuffer(mulaw_to_pcm16(encoded), dtype="<i2")
    assert np.max(np.abs(restored)) > 5000


class FakeTwilioSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_text(self, message):
        self.sent.append(json.loads(message))

    async def close(self, code=1000, reason=None):
        self.closed = True


def test_outgoing_audio_is_packetized_and_mark_ack_reports_playback():
    socket = FakeTwilioSocket()
    transport = TwilioCallTransport(socket, "CA111", "MZ111", settings, ACCOUNT_SID)

    async def scenario():
        await transport.send_bytes(np.zeros(3200, dtype="<i2").tobytes())
        await transport.send_json({"type": "audio_end"})

    run(scenario())
    media = [message for message in socket.sent if message["event"] == "media"]
    marks = [message for message in socket.sent if message["event"] == "mark"]
    assert media and marks
    assert all(message["streamSid"] == "MZ111" for message in media + marks)
    payload = b"".join(base64.b64decode(message["media"]["payload"]) for message in media)
    assert len(payload) == 1600
    assert all(len(base64.b64decode(message["media"]["payload"])) <= 160 for message in media)

    final_mark = next(name for name, state in transport.marks.items() if state[2])
    transport.receive_mark({"mark": {"name": final_mark}})
    received = [run(transport.receive()) for _ in range(3)]
    controls = [json.loads(message["text"]) for message in received]
    assert controls[0]["type"] == "playback_started"
    assert controls[1] == {"type": "playback_progress", "played_ms": 200}
    assert controls[2]["type"] == "playback_drained"


def test_clear_discards_late_audio_until_the_next_user_turn():
    socket = FakeTwilioSocket()
    transport = TwilioCallTransport(socket, "CA111", "MZ111", settings, ACCOUNT_SID)
    pcm = np.zeros(3200, dtype="<i2").tobytes()

    async def scenario():
        await transport.send_json({"type": "clear"})
        await transport.send_bytes(pcm)
        await transport.send_json({"type": "audio_end", "transient": True})
        events_after_clear = [message["event"] for message in socket.sent]
        await transport.send_json({"type": "transcript_final", "text": "next turn"})
        await transport.send_bytes(pcm)
        return events_after_clear

    events_after_clear = run(scenario())
    assert events_after_clear == ["clear"]
    assert any(message["event"] == "media" for message in socket.sent)


def test_a_listening_acknowledgement_still_plays_after_a_barge_in():
    socket = FakeTwilioSocket()
    transport = TwilioCallTransport(socket, "CA111", "MZ111", settings, ACCOUNT_SID)

    async def scenario():
        await transport.send_json({"type": "clear"})
        await transport.send_json({"type": "audio_start"})
        await transport.send_bytes(np.zeros(3200, dtype="<i2").tobytes())
        await transport.send_json({"type": "audio_end", "transient": True})

    run(scenario())
    assert [message["event"] for message in socket.sent][:2] == ["clear", "media"]


def test_a_phone_call_is_greeted_as_soon_as_it_listens_and_the_browser_is_not(monkeypatch):
    async def scenario():
        phone = Bench(monkeypatch, ScriptedLLM())
        await phone.session._on_control({"type": "start", "phone": True})
        await asyncio.sleep(0.05)
        assert phone.socket.audio and phone.socket.of_type("audio_start")
        assert phone.socket.of_type("audio_end")[-1] == {"type": "audio_end", "transient": True}
        assert settings.phone_greeting in phone.session.memory.render_state(NOW, settings.business_timezone)
        await phone.stop()

        browser = Bench(monkeypatch, ScriptedLLM())
        await browser.start()
        await asyncio.sleep(0.05)
        assert browser.socket.audio == b""
        assert settings.phone_greeting not in browser.session.memory.render_state(NOW, settings.business_timezone)
        await browser.stop()

    run(scenario())


def test_a_call_we_placed_gets_its_own_greeting_and_the_model_knows(monkeypatch):
    async def scenario():
        bench = Bench(monkeypatch, ScriptedLLM())
        await bench.session._on_control({"type": "start", "phone": True, "outbound": True})
        await asyncio.sleep(0.05)
        assert bench.socket.audio
        state = bench.session.memory.render_state(NOW, settings.business_timezone)
        assert settings.phone_outbound_greeting in state and settings.phone_greeting not in state
        assert "We phoned this customer" in state
        await bench.stop()

    run(scenario())


def test_the_outbound_mark_travels_from_the_webhook_to_the_session(monkeypatch):
    assert ET.fromstring(build_twiml("wss://voice.example.test/s")).find("./Connect/Stream/Parameter") is None
    parameter = ET.fromstring(build_twiml("wss://voice.example.test/s", outbound=True)).find("./Connect/Stream/Parameter")
    assert (parameter.get("name"), parameter.get("value")) == ("direction", "outbound")

    monkeypatch.setattr(twilio_module, "settings", twilio_settings())
    monkeypatch.setattr(twilio_module, "_valid_signature", lambda *args, **kwargs: True)
    monkeypatch.setattr(twilio_module, "VoiceSession", SessionProbe)
    SessionProbe.instances.clear()
    start = _start_event("CA111", "MZ111")
    start["start"]["customParameters"] = {"direction": "outbound"}
    with TestClient(make_app()) as client:
        with client.websocket_connect("/twilio/media-stream", headers={"x-twilio-signature": "test"}) as stream:
            stream.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            stream.send_json(start)
            stream.send_json({"event": "stop", "streamSid": "MZ111"})
    assert json.loads(SessionProbe.instances[0].messages[0]["text"]) == {"type": "start", "phone": True, "outbound": True}


def test_a_silent_phone_call_says_goodbye_and_is_not_stopped_under_it(monkeypatch):
    real_sleep = asyncio.sleep
    # the idle watchdog looks every 5 s; here it looks at once
    monkeypatch.setattr(asyncio, "sleep", lambda seconds, *args: real_sleep(0 if seconds >= 5 else seconds))

    async def scenario():
        bench = Bench(monkeypatch, ScriptedLLM())
        await bench.session._on_control({"type": "start", "phone": True})
        await real_sleep(0.05)
        greeting_bytes = len(bench.socket.audio)
        bench.session.last_activity -= settings.idle_timeout_seconds + 1
        await real_sleep(0.05)
        kinds = [event["type"] for event in bench.socket.events]
        assert kinds[-3:] == ["audio_start", "audio_end", "idle_timeout"]
        assert len(bench.socket.audio) > greeting_bytes  # the goodbye
        assert "clear" not in kinds and bench.session.state == "listening"  # nothing cuts the goodbye off
        await bench.stop()

    run(scenario())


def test_the_call_is_ended_only_after_twilio_has_played_the_goodbye(monkeypatch):
    ended = []
    monkeypatch.setattr(twilio_module, "_end_call", lambda account_sid, call_sid, config: ended.append((account_sid, call_sid)))
    socket = FakeTwilioSocket()
    transport = TwilioCallTransport(socket, "CA111", "MZ111", settings, ACCOUNT_SID)

    async def scenario():
        await transport.send_json({"type": "idle_timeout"})
        assert socket.sent[-1] == {"event": "mark", "streamSid": "MZ111", "mark": {"name": "hang-up"}}
        assert ended == []
        transport.receive_mark({"event": "mark", "streamSid": "MZ111", "mark": {"name": "hang-up"}})
        await transport.hang_up_task

    run(scenario())
    assert ended == [(ACCOUNT_SID, "CA111")] and not socket.closed

    def fail(*args):
        raise RuntimeError("Twilio cannot be reached")

    monkeypatch.setattr(twilio_module, "_end_call", fail)
    run(transport.hang_up())
    assert socket.closed  # Twilio then says the fallback sentence and hangs up


def test_a_phone_call_is_not_left_silent_when_the_conversation_stops_by_itself():
    socket = FakeTwilioSocket()
    transport = TwilioCallTransport(socket, "CA111", "MZ111", settings, ACCOUNT_SID)
    run(transport.send_json({"type": "state", "value": "listening"}))
    assert not socket.closed
    run(transport.send_json({"type": "state", "value": "idle"}))  # for example Deepgram was lost
    assert socket.closed

    socket = FakeTwilioSocket()
    transport = TwilioCallTransport(socket, "CA111", "MZ111", settings, ACCOUNT_SID)
    transport.offer_disconnect()  # the caller hung up: the clean-up that follows must not close it again
    run(transport.send_json({"type": "state", "value": "idle"}))
    assert not socket.closed


class SessionProbe:
    instances = []

    def __init__(self, transport, config, llm, service, input_rail, output_rail, instructions):
        self.transport = transport
        self.args = (config, llm, service, input_rail, output_rail, instructions)
        self.messages = []
        self.closed = False
        SessionProbe.instances.append(self)

    async def run(self):
        while True:
            message = await self.transport.receive()
            self.messages.append(message)
            if message["type"] == "websocket.disconnect":
                self.closed = True
                return


def _start_event(call_sid, stream_sid):
    return {
        "event": "start",
        "streamSid": stream_sid,
        "start": {
            "accountSid": ACCOUNT_SID,
            "callSid": call_sid,
            "streamSid": stream_sid,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }


def test_media_stream_creates_isolated_voice_sessions_and_cleans_up(monkeypatch):
    monkeypatch.setattr(twilio_module, "settings", twilio_settings())
    monkeypatch.setattr(twilio_module, "_valid_signature", lambda *args, **kwargs: True)
    monkeypatch.setattr(twilio_module, "VoiceSession", SessionProbe)
    SessionProbe.instances.clear()
    api = make_app()

    with TestClient(api) as client:
        with client.websocket_connect(
            "/twilio/media-stream", headers={"x-twilio-signature": "test"}
        ) as first:
            first.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
            first.send_json(_start_event("CA111", "MZ111"))
            first.send_json({"event": "media", "streamSid": "MZ111", "media": {"payload": "%%%"}})
            first.send_json({
                "event": "media",
                "streamSid": "MZ111",
                "media": {"payload": base64.b64encode(b"\xff" * 160).decode("ascii")},
            })
            with client.websocket_connect(
                "/twilio/media-stream", headers={"x-twilio-signature": "test"}
            ) as second:
                second.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
                second.send_json(_start_event("CA222", "MZ222"))
                second.send_json({
                    "event": "media",
                    "streamSid": "MZ222",
                    "media": {"payload": base64.b64encode(b"\x00" * 160).decode("ascii")},
                })
                second.send_json({"event": "stop", "streamSid": "MZ222"})
            first.send_json({"event": "stop", "streamSid": "MZ111"})

    assert len(SessionProbe.instances) == 2
    first_session, second_session = SessionProbe.instances
    assert first_session.transport is not second_session.transport
    assert json.loads(first_session.messages[0]["text"]) == {"type": "start", "phone": True}
    assert first_session.transport.call_sid == "CA111"
    assert first_session.transport.account_sid == ACCOUNT_SID
    assert second_session.transport.call_sid == "CA222"
    assert first_session.transport.stream_sid == "MZ111"
    assert second_session.transport.stream_sid == "MZ222"
    first_audio = b"".join(message.get("bytes", b"") for message in first_session.messages)
    second_audio = b"".join(message.get("bytes", b"") for message in second_session.messages)
    assert first_audio and second_audio and first_audio != second_audio
    assert first_session.closed and second_session.closed
    assert api.state.twilio_calls == {}


def test_media_stream_rejects_unexpected_audio_format(monkeypatch):
    monkeypatch.setattr(twilio_module, "settings", twilio_settings())
    monkeypatch.setattr(twilio_module, "_valid_signature", lambda *args, **kwargs: True)
    api = make_app()
    invalid = _start_event("CA111", "MZ111")
    invalid["start"]["mediaFormat"]["encoding"] = "audio/linear16"
    with TestClient(api) as client:
        with client.websocket_connect(
            "/twilio/media-stream", headers={"x-twilio-signature": "test"}
        ) as websocket:
            websocket.send_json({"event": "connected", "protocol": "Call"})
            websocket.send_json(invalid)
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
