"""Audio conversion between Twilio Media Streams and the existing voice pipeline."""
import numpy as np
import soxr


TWILIO_SAMPLE_RATE = 8000
_MULAW_BIAS = 0x84
_MULAW_CLIP = 32635
_SEGMENT_ENDS = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)


def _decode_mulaw_sample(value):
    value = (~value) & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = ((mantissa << 3) + _MULAW_BIAS) << exponent
    sample -= _MULAW_BIAS
    return -sample if sign else sample


def _encode_mulaw_sample(sample):
    sample = int(sample)
    mask = 0xFF if sample >= 0 else 0x7F
    if sample < 0:
        sample = -sample
    sample = min(sample, _MULAW_CLIP) + _MULAW_BIAS

    segment = 0
    while segment < len(_SEGMENT_ENDS) and sample > _SEGMENT_ENDS[segment]:
        segment += 1
    if segment >= len(_SEGMENT_ENDS):
        return 0x7F ^ mask

    encoded = (segment << 4) | ((sample >> (segment + 3)) & 0x0F)
    return encoded ^ mask


_MULAW_DECODE_TABLE = np.array([_decode_mulaw_sample(value) for value in range(256)], dtype=np.int16)


def mulaw_to_pcm16(audio):
    """Decode raw G.711 mu-law bytes to little-endian signed 16-bit PCM samples."""
    if not audio:
        return b""
    encoded = np.frombuffer(audio, dtype=np.uint8)
    return _MULAW_DECODE_TABLE[encoded].astype("<i2", copy=False).tobytes()


def pcm16_to_mulaw(audio):
    """Encode little-endian signed 16-bit PCM bytes as raw G.711 mu-law."""
    usable_bytes = len(audio) - len(audio) % 2
    if usable_bytes == 0:
        return b""
    samples = np.frombuffer(audio[:usable_bytes], dtype="<i2")
    return bytes(_encode_mulaw_sample(sample) for sample in samples)


class TwilioAudioConverter:
    """Keep stateful resamplers so streamed chunk boundaries do not reset the filters."""

    def __init__(self, stt_sample_rate, tts_sample_rate):
        self.stt_sample_rate = int(stt_sample_rate)
        self.tts_sample_rate = int(tts_sample_rate)
        if self.stt_sample_rate <= 0 or self.tts_sample_rate <= 0:
            raise ValueError("STT and TTS sample rates must be positive")
        self._inbound_resampler = self._new_resampler(TWILIO_SAMPLE_RATE, self.stt_sample_rate)
        self._outbound_resampler = self._new_resampler(self.tts_sample_rate, TWILIO_SAMPLE_RATE)

    @staticmethod
    def _new_resampler(input_rate, output_rate):
        if input_rate == output_rate:
            return None
        return soxr.ResampleStream(input_rate, output_rate, 1, dtype="int16", quality="HQ")

    def decode_inbound(self, mulaw_audio):
        """Convert one raw Twilio mu-law chunk to PCM16 at the configured Deepgram rate."""
        pcm = mulaw_to_pcm16(mulaw_audio)
        if not pcm or self._inbound_resampler is None:
            return pcm
        samples = np.frombuffer(pcm, dtype="<i2")
        converted = self._inbound_resampler.resample_chunk(samples, last=False)
        return converted.astype("<i2", copy=False).tobytes()

    def finish_inbound(self):
        """Flush resampler tail at call end and prepare for a possible next stream."""
        if self._inbound_resampler is None:
            return b""
        tail = self._inbound_resampler.resample_chunk(np.empty(0, dtype=np.int16), last=True)
        self._inbound_resampler = self._new_resampler(TWILIO_SAMPLE_RATE, self.stt_sample_rate)
        return tail.astype("<i2", copy=False).tobytes()

    def encode_outgoing(self, pcm_audio):
        """Convert one PCM16 TTS chunk to raw Twilio mu-law audio at 8 kHz."""
        usable_bytes = len(pcm_audio) - len(pcm_audio) % 2
        if usable_bytes == 0:
            return b""
        pcm_audio = pcm_audio[:usable_bytes]
        if self._outbound_resampler is None:
            return pcm16_to_mulaw(pcm_audio)
        samples = np.frombuffer(pcm_audio, dtype="<i2")
        converted = self._outbound_resampler.resample_chunk(samples, last=False)
        return pcm16_to_mulaw(converted.astype("<i2", copy=False).tobytes())

    def finish_outgoing(self):
        """Flush one reply's filter tail so the final audio is not cut off."""
        if self._outbound_resampler is None:
            return b""
        tail = self._outbound_resampler.resample_chunk(np.empty(0, dtype=np.int16), last=True)
        self._outbound_resampler = self._new_resampler(self.tts_sample_rate, TWILIO_SAMPLE_RATE)
        return pcm16_to_mulaw(tail.astype("<i2", copy=False).tobytes())

    def discard_outgoing(self):
        """Drop buffered samples after Twilio clears playback during an interruption."""
        self._outbound_resampler = self._new_resampler(self.tts_sample_rate, TWILIO_SAMPLE_RATE)
