"""SIP/RTP call handling for FreePBX Assist.

A lightweight SIP endpoint (built on voip-utils, the same library Home
Assistant's core ``voip`` integration uses) answers calls coming from the
PBX, identifies the room from the caller ID (the extension number in the
SIP From header), streams the caller's speech into an Assist pipeline, and
plays the pipeline's TTS response back into the call.

Each extension gets its own device in the device registry, assigned to an
area.  Home Assistant's Assist only scopes an unqualified command like
"turn off the lights" to a caller's area when the *sentence itself*
contains an area phrase (e.g. "... in the lounge") - passing a device_id
alone is not enough.  So speech-to-text and intent/TTS are run as two
separate pipeline stages here, and the caller's room name is appended to
the transcript in between.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import struct
import time
import wave
from functools import lru_cache, partial

from voip_utils import (
    CallInfo,
    RtcpState,
    RtpDatagramProtocol,
    SdpInfo,
    VoipDatagramProtocol,
)

from homeassistant.components import stt, tts
from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineInput,
    PipelineNotFound,
    PipelineRun,
    PipelineStage,
    async_get_pipeline,
    async_get_pipelines,
)

try:  # exported from the package root in recent HA versions
    from homeassistant.components.assist_pipeline import AudioSettings
except ImportError:  # pragma: no cover - older HA
    from homeassistant.components.assist_pipeline.pipeline import AudioSettings

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session, device_registry as dr

from .const import (
    AUDIO_TIMEOUT,
    CHANNELS,
    CONF_ALLOWED_EXTENSIONS,
    CONF_PBX_HOST,
    CONF_PIPELINE_MAP,
    CONF_PIPELINE_TIMEOUT,
    DEFAULT_PIPELINE_TIMEOUT,
    DOMAIN,
    EXTENSION_AREAS,
    MAX_CALL_SECONDS,
    MAX_ERRORS,
    RATE,
    WIDTH,
)

_LOGGER = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _tone(freq_hz: float, seconds: float, volume: float = 0.3) -> bytes:
    """Generate a 16 kHz mono s16le sine tone."""
    num_samples = int(RATE * seconds)
    amp = volume * 32767
    return b"".join(
        struct.pack("<h", int(amp * math.sin(2 * math.pi * freq_hz * i / RATE)))
        for i in range(num_samples)
    )


def _listen_tone() -> bytes:
    """Short high beep: 'I'm listening'."""
    return _tone(880.0, 0.18)


def _error_tone() -> bytes:
    """Lower double beep: 'that didn't work'."""
    beep = _tone(320.0, 0.15)
    gap = bytes(int(RATE * 0.1) * WIDTH)
    return beep + gap + beep


def _parse_list(raw: str) -> list[str]:
    """Parse a comma/space separated list of extensions."""
    return [item for item in re.split(r"[\s,;]+", raw or "") if item]


def _parse_map(raw: str) -> dict[str, str]:
    """Parse 'extension = pipeline name' lines into a dict."""
    result: dict[str, str] = {}
    for line in (raw or "").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def extension_from_call_info(call_info: CallInfo) -> str:
    """Get the calling extension (caller ID number) from SIP call info."""
    endpoint = getattr(call_info, "caller_endpoint", None)
    username = getattr(endpoint, "username", None) if endpoint else None
    if username:
        return str(username)

    # Fallback: parse the From header directly
    headers = getattr(call_info, "headers", None) or {}
    from_header = headers.get("from", "")
    match = re.search(r"sip:([^@;>]+)", from_header)
    if match:
        return match.group(1)

    return str(getattr(call_info, "caller_ip", "unknown"))


class FreePBXVoipProtocol(VoipDatagramProtocol):
    """UDP server that answers SIP calls from the PBX."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        super().__init__(
            sdp_info=SdpInfo(
                username="homeassistant",
                id=int(time.time()),
                session_name="freepbx_assist",
                version=1,
            ),
            valid_protocol_factory=self._make_protocol,
        )

    def is_valid_call(self, call_info: CallInfo) -> bool:
        """Accept only calls from the configured PBX / extensions."""
        pbx_host = (self._entry.data.get(CONF_PBX_HOST) or "").strip()
        caller_ip = str(getattr(call_info, "caller_ip", ""))
        if pbx_host and caller_ip != pbx_host:
            _LOGGER.warning(
                "Rejected SIP call from %s (expected PBX at %s)", caller_ip, pbx_host
            )
            return False

        extension = extension_from_call_info(call_info)
        allowed = _parse_list(self._entry.options.get(CONF_ALLOWED_EXTENSIONS, ""))
        if allowed and extension not in allowed:
            _LOGGER.warning(
                "Rejected call from extension %s (not in allowed list %s)",
                extension,
                allowed,
            )
            return False

        return True

    def _make_protocol(
        self, call_info: CallInfo, rtcp_state: RtcpState | None = None
    ) -> RtpDatagramProtocol:
        """Create the RTP handler for an accepted call."""
        extension = extension_from_call_info(call_info)

        # One device per extension; its area is what scopes Assist commands
        # from this caller to the right room.
        area = EXTENSION_AREAS.get(extension)
        dev_reg = dr.async_get(self.hass)
        device = dev_reg.async_get_or_create(
            config_entry_id=self._entry.entry_id,
            identifiers={(DOMAIN, extension)},
            name=f"{area} phone ({extension})" if area else f"FreePBX extension {extension}",
            manufacturer="FreePBX",
            model="SIP extension",
            suggested_area=area,
        )

        pipeline_id = self._resolve_pipeline_id(extension)
        pipeline_timeout = self._entry.options.get(
            CONF_PIPELINE_TIMEOUT, DEFAULT_PIPELINE_TIMEOUT
        )

        _LOGGER.info(
            "Answering Assist call from extension %s (device %s, area %s)",
            extension,
            device.id,
            device.area_id or "not set",
        )

        return AssistCallProtocol(
            self.hass,
            call_info,
            extension=extension,
            device_id=device.id,
            pipeline_id=pipeline_id,
            pipeline_timeout=float(pipeline_timeout),
        )

    def _resolve_pipeline_id(self, extension: str) -> str | None:
        """Map an extension to an Assist pipeline id via the options mapping."""
        pipeline_map = _parse_map(self._entry.options.get(CONF_PIPELINE_MAP, ""))
        wanted = pipeline_map.get(extension)
        if not wanted:
            return None  # use the default pipeline
        for pipeline in async_get_pipelines(self.hass):
            if pipeline.name.lower() == wanted.lower():
                return pipeline.id
        _LOGGER.warning(
            "Pipeline '%s' for extension %s not found; using default", wanted, extension
        )
        return None


class AssistCallProtocol(RtpDatagramProtocol):
    """Handles one call: audio in -> Assist pipeline -> TTS audio out."""

    def __init__(
        self,
        hass: HomeAssistant,
        call_info: CallInfo,
        *,
        extension: str,
        device_id: str,
        pipeline_id: str | None,
        pipeline_timeout: float = DEFAULT_PIPELINE_TIMEOUT,
    ) -> None:
        super().__init__(
            rate=RATE,
            width=WIDTH,
            channels=CHANNELS,
            opus_payload_type=call_info.opus_payload_type,
        )
        self.hass = hass
        self._extension = extension
        self._device_id = device_id
        self._room = EXTENSION_AREAS.get(extension)
        self._pipeline_id = pipeline_id
        self._pipeline_timeout = pipeline_timeout

        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._listening = False
        self._closed = False
        self._last_chunk_time = time.monotonic()
        self._loop_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

        # per-turn pipeline state
        self._conversation_id: str | None = None
        self._last_stt_text = ""
        self._tts_media_id: str | None = None
        self._had_error = False

    # -- asyncio protocol callbacks -------------------------------------

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        self._last_chunk_time = time.monotonic()
        self._loop_task = self.hass.async_create_background_task(
            self._call_loop(), name=f"freepbx_assist call {self._extension}"
        )
        self._watchdog_task = self.hass.async_create_background_task(
            self._watchdog(), name=f"freepbx_assist watchdog {self._extension}"
        )

    def connection_lost(self, exc) -> None:
        self._closed = True
        _LOGGER.debug("Call from %s: RTP connection closed", self._extension)
        try:
            super().connection_lost(exc)
        except AttributeError:
            pass

    def on_chunk(self, audio_bytes: bytes) -> None:
        """Receive decoded 16 kHz mono PCM audio from the caller."""
        self._last_chunk_time = time.monotonic()
        if self._listening:
            self._audio_queue.put_nowait(audio_bytes)

    # -- call flow ------------------------------------------------------

    async def _call_loop(self) -> None:
        call_start = time.monotonic()
        errors = 0
        try:
            await asyncio.sleep(0.3)  # let RTP settle before the greeting
            await self._send_pcm(_listen_tone(), silence_before=0.1)

            while not self._closed:
                if time.monotonic() - call_start > MAX_CALL_SECONDS:
                    _LOGGER.info(
                        "Call from %s reached max length; hanging up", self._extension
                    )
                    break

                self._clear_audio_queue()
                self._listening = True
                result = await self._run_pipeline_once()
                self._listening = False

                if result == "hangup" or self._closed:
                    break
                if result == "error":
                    errors += 1
                    await self._send_pcm(_error_tone(), silence_before=0.2)
                    if errors >= MAX_ERRORS:
                        break
                    continue

                errors = 0
                if self._tts_media_id:
                    await self._play_tts(self._tts_media_id)
                # brief beep to signal we're listening again
                await self._send_pcm(_listen_tone(), silence_before=0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Unexpected error in call from %s", self._extension)
        finally:
            self._listening = False
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()
            self._close()

    async def _watchdog(self) -> None:
        """Hang up if the caller's RTP stream stops (caller hung up)."""
        while not self._closed:
            await asyncio.sleep(2)
            if time.monotonic() - self._last_chunk_time > AUDIO_TIMEOUT:
                _LOGGER.debug(
                    "No RTP audio from %s for %ss; ending call",
                    self._extension,
                    AUDIO_TIMEOUT,
                )
                if self._loop_task and not self._loop_task.done():
                    self._loop_task.cancel()
                self._close()
                return

    async def _run_pipeline_once(self) -> str:
        """Run one voice command through the Assist pipeline.

        Speech-to-text and intent/TTS are run as two separate pipeline
        stages so the caller's room name can be appended to the transcript
        before intent recognition - see the module docstring for why this
        is needed to get room-scoped commands.

        Returns "ok", "error", or "hangup".
        """
        self._tts_media_id = None
        self._had_error = False
        self._last_stt_text = ""

        async def stt_stream():
            while True:
                yield await self._audio_queue.get()

        try:
            pipeline = async_get_pipeline(self.hass, pipeline_id=self._pipeline_id)
        except PipelineNotFound:
            _LOGGER.error("Assist pipeline not found for extension %s", self._extension)
            return "hangup"

        with chat_session.async_get_chat_session(
            self.hass, self._conversation_id
        ) as session:
            self._conversation_id = session.conversation_id

            stt_input = PipelineInput(
                session=session,
                device_id=self._device_id,
                stt_metadata=stt.SpeechMetadata(
                    language="",  # filled in from the pipeline
                    format=stt.AudioFormats.WAV,
                    codec=stt.AudioCodecs.PCM,
                    bit_rate=stt.AudioBitRates.BITRATE_16,
                    sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
                    channel=stt.AudioChannels.CHANNEL_MONO,
                ),
                stt_stream=stt_stream(),
                run=PipelineRun(
                    self.hass,
                    context=Context(),
                    pipeline=pipeline,
                    start_stage=PipelineStage.STT,
                    end_stage=PipelineStage.STT,
                    event_callback=self._on_pipeline_event,
                    audio_settings=AudioSettings(is_vad_enabled=True),
                ),
            )
            try:
                await asyncio.wait_for(
                    stt_input.execute(validate=True), timeout=self._pipeline_timeout
                )
            except (asyncio.TimeoutError, TimeoutError):
                _LOGGER.info(
                    "No command from %s within %ss; hanging up",
                    self._extension,
                    self._pipeline_timeout,
                )
                return "hangup"
            except Exception:
                _LOGGER.exception(
                    "Speech-to-text failed for extension %s", self._extension
                )
                return "error"

            intent_text = self._last_stt_text
            if self._room:
                intent_text = f"{intent_text} in the {self._room.lower()}"

            intent_input = PipelineInput(
                session=session,
                device_id=self._device_id,
                intent_input=intent_text,
                run=PipelineRun(
                    self.hass,
                    context=Context(),
                    pipeline=pipeline,
                    start_stage=PipelineStage.INTENT,
                    end_stage=PipelineStage.TTS,
                    event_callback=self._on_pipeline_event,
                    tts_audio_output="wav",
                ),
            )
            try:
                await intent_input.execute(validate=True)
            except Exception:
                _LOGGER.exception(
                    "Intent/TTS processing failed for extension %s", self._extension
                )
                return "error"

        return "error" if self._had_error else "ok"

    def _on_pipeline_event(self, event: PipelineEvent) -> None:
        data = event.data or {}
        if event.type == PipelineEventType.STT_END:
            text = data.get("stt_output", {}).get("text", "")
            self._last_stt_text = text
            _LOGGER.info("Extension %s said: %s", self._extension, text)
        elif event.type == PipelineEventType.INTENT_END:
            intent_output = data.get("intent_output", {})
            self._conversation_id = intent_output.get("conversation_id")
            speech = (
                intent_output.get("response", {})
                .get("speech", {})
                .get("plain", {})
                .get("speech", "")
            )
            if speech:
                _LOGGER.info("Reply to extension %s: %s", self._extension, speech)
        elif event.type == PipelineEventType.TTS_END:
            tts_output = data.get("tts_output")
            if tts_output:
                self._tts_media_id = tts_output.get("media_id")
        elif event.type == PipelineEventType.ERROR:
            self._had_error = True
            _LOGGER.warning(
                "Pipeline error for extension %s: %s (%s)",
                self._extension,
                data.get("message"),
                data.get("code"),
            )

    # -- audio out ------------------------------------------------------

    async def _play_tts(self, media_id: str) -> None:
        """Fetch the pipeline's TTS result and play it into the call."""
        try:
            _extension, data = await tts.async_get_media_source_audio(
                self.hass, media_id
            )
        except Exception:
            _LOGGER.exception("Failed to fetch TTS audio %s", media_id)
            return

        try:
            with wave.open(io.BytesIO(data), "rb") as wav_file:
                frames = wav_file.readframes(wav_file.getnframes())
                rate = wav_file.getframerate()
                width = wav_file.getsampwidth()
                channels = wav_file.getnchannels()
        except wave.Error:
            _LOGGER.warning(
                "TTS output was not WAV; check that your TTS engine supports "
                "16 kHz mono WAV output"
            )
            return

        await self._send_pcm(
            frames, rate=rate, width=width, channels=channels, silence_before=0.3
        )

    async def _send_pcm(
        self,
        audio: bytes,
        rate: int = RATE,
        width: int = WIDTH,
        channels: int = CHANNELS,
        silence_before: float = 0.0,
    ) -> None:
        """Send PCM audio to the caller (blocking send runs in executor)."""
        if self._closed or self.transport is None:
            return
        try:
            await self.hass.async_add_executor_job(
                partial(
                    self.send_audio,
                    audio,
                    rate=rate,
                    width=width,
                    channels=channels,
                    silence_before=silence_before,
                )
            )
        except Exception:
            if not self._closed:
                _LOGGER.exception("Failed to send audio to extension %s", self._extension)

    # -- teardown -------------------------------------------------------

    def _clear_audio_queue(self) -> None:
        while not self._audio_queue.empty():
            self._audio_queue.get_nowait()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Closing the RTP transport stops our media; Asterisk's rtp_timeout
        # (see the FreePBX config) then tears the call down if the caller
        # hasn't already hung up.
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # pragma: no cover
                pass
