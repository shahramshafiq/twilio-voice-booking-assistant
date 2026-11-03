"""Twilio webhook and bidirectional Media Streams adapter for the existing voice session."""
import asyncio
import base64
import binascii
import json
import logging
import uuid
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from .config import settings
from .session import VoiceSession
from .twilio_audio import TwilioAudioConverter

log = logging.getLogger("twilio")
router = APIRouter()

_MEDIA_FRAME_BYTES = 160  # 20 ms of mu-law at 8 kHz
_MARK_INTERVAL_BYTES = 800  # playback progress every 100 ms
_MAX_MEDIA_PAYLOAD_CHARS = 65536
_HANG_UP_MARK = "hang-up"  # Twilio returns it once everything sent before it has been played


def _public_origin(configured_url):
    if not configured_url:
        return None
    parsed = urlsplit(configured_url.strip())
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"https://{parsed.netloc}"


def _signature_url(origin, path, query="", websocket=False):
    parsed = urlsplit(origin)
    scheme = "wss" if websocket else "https"
    url = f"{scheme}://{parsed.netloc}{path}"
    if query:
        url += "?" + query
    return url


def _valid_signature(auth_token, signature, url, parameters, websocket=False):
    if not auth_token or not signature:
        return False
    validator = RequestValidator(auth_token)
    candidates = [url]
    if websocket and not url.endswith("/"):
        candidates.append(url + "/")
    return any(validator.validate(candidate, parameters, signature) for candidate in candidates)


def build_twiml(stream_url):
    """Return TwiML that opens a bidirectional stream and has a fallback if it closes early."""
    response = VoiceResponse()
    response.connect().stream(url=stream_url)
    response.say("Sorry, the voice assistant connection ended. Please try your call again.")
    response.hangup()
    return str(response)


def _end_call(account_sid, call_sid, config):
    """Ask Twilio's REST API to end the call. This request blocks, so it runs in a thread."""
    http_client = TwilioHttpClient(timeout=config.twilio_api_timeout_seconds)
    Client(account_sid, config.twilio_auth_token, http_client=http_client).calls(call_sid).update(status="completed")


class TwilioCallTransport:
    """Expose a Twilio Media Stream as the small WebSocket interface VoiceSession already uses."""

    def __init__(self, websocket, call_sid, stream_sid, config, account_sid):
        self.websocket = websocket
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.config = config
        self.account_sid = account_sid
        self.hang_up_task = None
        self.converter = TwilioAudioConverter(config.deepgram_sample_rate, config.tts_sample_rate)
        self.incoming = asyncio.Queue()
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.generation = 0
        self.started_generation = None
        self.output_bytes = 0
        self.bytes_since_mark = 0
        self.marks = {}
        self.suppress_audio = False

    async def receive(self):
        return await self.incoming.get()

    def offer_audio(self, pcm_audio):
        if pcm_audio and not self.closed:
            self.incoming.put_nowait({"type": "websocket.receive", "bytes": pcm_audio})

    def offer_control(self, control):
        if not self.closed:
            self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(control)})

    def offer_disconnect(self):
        if not self.closed:
            self.closed = True
            self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    async def send_bytes(self, pcm_audio):
        if self.closed or self.suppress_audio:
            return
        generation = self.generation
        mulaw_audio = self.converter.encode_outgoing(pcm_audio)
        if not mulaw_audio or self.closed:
            return
        async with self.send_lock:
            if self.closed or self.suppress_audio or generation != self.generation:
                return
            for offset in range(0, len(mulaw_audio), _MEDIA_FRAME_BYTES):
                frame = mulaw_audio[offset:offset + _MEDIA_FRAME_BYTES]
                await self._send_media_locked(frame)
                self.output_bytes += len(frame)
                self.bytes_since_mark += len(frame)
                if self.bytes_since_mark >= _MARK_INTERVAL_BYTES:
                    await self._send_mark_locked(final=False, report=True)

    async def send_json(self, event):
        kind = event.get("type")
        if kind == "clear":
            self.suppress_audio = True
            self.generation += 1
            self.started_generation = None
            self.output_bytes = 0
            self.bytes_since_mark = 0
            self.marks.clear()
            self.converter.discard_outgoing()
            if not self.closed:
                async with self.send_lock:
                    await self._send_locked({"event": "clear", "streamSid": self.stream_sid})
        elif kind in ("transcript_final", "audio_start"):
            self.suppress_audio = False  # a new caller turn, or a ready-made sound such as "Okay."
        elif kind == "audio_end":
            if self.suppress_audio:
                self.converter.discard_outgoing()
                self.started_generation = None
                self.output_bytes = 0
                self.bytes_since_mark = 0
                return
            tail = self.converter.finish_outgoing()
            async with self.send_lock:
                if self.closed:
                    return
                await self._send_audio_locked(tail)
                await self._send_mark_locked(final=not event.get("transient", False), report=not event.get("transient", False))
                self.started_generation = None
                self.output_bytes = 0
                self.bytes_since_mark = 0
        elif kind == "idle_timeout":
            # the goodbye was sent just before this: the call ends when Twilio returns the mark after it
            async with self.send_lock:
                if not self.closed:
                    await self._send_locked({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": _HANG_UP_MARK}})

    async def hang_up(self):
        """End the call. If the REST request fails, close the stream instead: Twilio then says the TwiML
        fallback sentence and hangs up."""
        try:
            await asyncio.to_thread(_end_call, self.account_sid, self.call_sid, self.config)
            log.info("hung up call %s", self.call_sid)
        except Exception as exc:
            log.warning("could not end call %s through the REST API (%s: %s), closing the stream instead",
                        self.call_sid, type(exc).__name__, exc)
            try:
                await self.websocket.close()
            except Exception as close_error:
                log.warning("could not close the stream of call %s: %s", self.call_sid, type(close_error).__name__)

    def receive_mark(self, event):
        name = (event.get("mark") or {}).get("name")
        if name == _HANG_UP_MARK:
            if self.hang_up_task is None:
                self.hang_up_task = asyncio.create_task(self.hang_up())
            return
        marker = self.marks.pop(name, None) if isinstance(name, str) else None
        if marker is None:
            return
        generation, played_ms, final, report = marker
        if generation != self.generation:
            return
        if report:
            self.offer_control({"type": "playback_progress", "played_ms": played_ms})
        if final:
            self.offer_control({"type": "playback_drained"})

    async def _send_audio_locked(self, mulaw_audio):
        for offset in range(0, len(mulaw_audio), _MEDIA_FRAME_BYTES):
            frame = mulaw_audio[offset:offset + _MEDIA_FRAME_BYTES]
            await self._send_media_locked(frame)
            self.output_bytes += len(frame)
            self.bytes_since_mark += len(frame)
            if self.bytes_since_mark >= _MARK_INTERVAL_BYTES:
                await self._send_mark_locked(final=False, report=True)

    async def _send_media_locked(self, frame):
        if self.started_generation != self.generation:
            self.started_generation = self.generation
            self.offer_control({"type": "playback_started"})
        encoded = base64.b64encode(frame).decode("ascii")
        await self._send_locked({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": encoded},
        })

    async def _send_mark_locked(self, final, report):
        self.bytes_since_mark = 0
        name = f"{self.generation}-{uuid.uuid4().hex}"
        played_ms = round(self.output_bytes / 8)
        self.marks[name] = (self.generation, played_ms, final, report)
        await self._send_locked({
            "event": "mark",
            "streamSid": self.stream_sid,
            "mark": {"name": name},
        })

    async def _send_locked(self, message):
        await self.websocket.send_text(json.dumps(message, separators=(",", ":")))


async def _read_event(websocket):
    message = await websocket.receive()
    if message.get("type") == "websocket.disconnect":
        return None
    raw = message.get("text")
    if raw is None:
        return {}
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return event if isinstance(event, dict) else {}


def _close_reason(websocket, code=1008, reason="Invalid Twilio Media Streams request"):
    return websocket.close(code=code, reason=reason)


def _valid_start(event, config):
    start = event.get("start")
    if not isinstance(start, dict):
        return None
    call_sid = start.get("callSid")
    stream_sid = start.get("streamSid") or event.get("streamSid")
    account_sid = start.get("accountSid")
    media_format = start.get("mediaFormat") or {}
    tracks = start.get("tracks") or []
    if not call_sid or not stream_sid or not account_sid or "inbound" not in tracks:
        return None
    if event.get("streamSid") and event["streamSid"] != stream_sid:
        return None
    if config.twilio_account_sid and account_sid != config.twilio_account_sid:
        return None
    if (
        media_format.get("encoding") != "audio/x-mulaw"
        or media_format.get("sampleRate") != 8000
        or media_format.get("channels") != 1
    ):
        return None
    return call_sid, stream_sid, account_sid


@router.post("/twilio/incoming")
async def incoming_call(request: Request):
    origin = _public_origin(settings.twilio_public_base_url)
    if not settings.twilio_auth_token or origin is None:
        raise HTTPException(status_code=503, detail="Twilio incoming calls are not configured.")

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Expected a form-encoded Twilio request.")
    try:
        body = (await request.body()).decode("utf-8")
        parameters = dict(parse_qsl(body, keep_blank_values=True))
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="The Twilio request body is invalid.")

    query = request.scope.get("query_string", b"").decode("ascii", errors="ignore")
    url = _signature_url(origin, request.url.path, query)
    signature = request.headers.get("x-twilio-signature", "")
    if not _valid_signature(settings.twilio_auth_token, signature, url, parameters):
        raise HTTPException(status_code=403, detail="Invalid Twilio request signature.")

    if settings.twilio_account_sid and parameters.get("AccountSid") != settings.twilio_account_sid:
        raise HTTPException(status_code=403, detail="The Twilio account does not match configuration.")
    if not parameters.get("CallSid") or not parameters.get("To"):
        raise HTTPException(status_code=400, detail="The Twilio call request is missing required fields.")
    if settings.twilio_phone_number and parameters.get("To") != settings.twilio_phone_number:
        raise HTTPException(status_code=403, detail="The called number does not match configuration.")

    stream_url = _signature_url(origin, "/twilio/media-stream", websocket=True)
    return Response(content=build_twiml(stream_url), media_type="application/xml")


@router.websocket("/twilio/media-stream")
async def media_stream(websocket: WebSocket):
    origin = _public_origin(settings.twilio_public_base_url)
    stream_url = _signature_url(origin, websocket.url.path, websocket.url.query, websocket=True) if origin else ""
    signature = websocket.headers.get("x-twilio-signature", "")
    if not settings.twilio_auth_token or not stream_url or not _valid_signature(
        settings.twilio_auth_token, signature, stream_url, {}, websocket=True
    ):
        await _close_reason(websocket)
        return

    await websocket.accept()
    transport = None
    session_task = None
    call_sid = None
    active_calls = getattr(websocket.app.state, "twilio_calls", None)
    if active_calls is None:
        active_calls = {}
        websocket.app.state.twilio_calls = active_calls

    try:
        connected = await asyncio.wait_for(_read_event(websocket), timeout=10)
        if not connected or connected.get("event") != "connected" or connected.get("protocol") not in (None, "Call"):
            await _close_reason(websocket)
            return

        start_event = await asyncio.wait_for(_read_event(websocket), timeout=10)
        call_info = _valid_start(start_event or {}, settings) if start_event else None
        if call_info is None:
            await _close_reason(websocket)
            return
        call_sid, stream_sid, account_sid = call_info
        if call_sid in active_calls:
            await _close_reason(websocket, reason="A stream for this call is already active")
            return
        if settings.deepgram_encoding != "linear16":
            await _close_reason(websocket, code=1011, reason="The STT audio format is not supported")
            return

        state = websocket.app.state
        transport = TwilioCallTransport(websocket, call_sid, stream_sid, settings, account_sid)
        session = VoiceSession(
            transport,
            settings,
            state.llm,
            state.service,
            state.input_rail,
            state.output_rail,
            state.instructions,
        )
        active_calls[call_sid] = session
        session_task = asyncio.create_task(session.run())
        transport.offer_control({"type": "start", "phone": True})
        log.info("Twilio call %s stream %s connected", call_sid, stream_sid)

        while not session_task.done():
            event = await _read_event(websocket)
            if event is None:
                break
            kind = event.get("event")
            event_stream_sid = event.get("streamSid")
            if kind == "media":
                if event_stream_sid != stream_sid:
                    await _close_reason(websocket)
                    break
                media = event.get("media") or {}
                payload = media.get("payload")
                if not isinstance(payload, str) or not payload or len(payload) > _MAX_MEDIA_PAYLOAD_CHARS:
                    log.warning("discarded invalid media payload on call %s", call_sid)
                    continue
                try:
                    mulaw_audio = base64.b64decode(payload, validate=True)
                    pcm_audio = transport.converter.decode_inbound(mulaw_audio)
                except (binascii.Error, ValueError, TypeError):
                    log.warning("discarded malformed media payload on call %s", call_sid)
                    continue
                transport.offer_audio(pcm_audio)
            elif kind == "mark":
                if event_stream_sid == stream_sid:
                    transport.receive_mark(event)
            elif kind == "stop":
                if event_stream_sid and event_stream_sid != stream_sid:
                    log.warning("ignored stop for a different Twilio stream on call %s", call_sid)
                break
            elif kind in ("dtmf", "connected"):
                continue
            else:
                log.debug("ignored Twilio event %s on call %s", kind, call_sid)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        log.info("Twilio stream ended during setup or disconnected for call %s", call_sid or "unknown")
    except Exception as exc:
        log.error("Twilio stream failed for call %s: %s", call_sid or "unknown", type(exc).__name__)
    finally:
        if transport is not None:
            try:
                transport.offer_audio(transport.converter.finish_inbound())
            except Exception as exc:
                log.warning("could not flush inbound audio for call %s: %s", call_sid, type(exc).__name__)
            transport.offer_disconnect()
        if session_task is not None:
            try:
                await session_task
            except Exception as exc:
                log.warning("voice session cleanup failed for call %s: %s", call_sid, type(exc).__name__)
        if call_sid is not None and active_calls.get(call_sid) is not None:
            active_calls.pop(call_sid, None)
            log.info("Twilio call %s stream ended", call_sid)
