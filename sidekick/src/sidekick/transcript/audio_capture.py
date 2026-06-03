"""WASAPI loopback audio capture — records system audio output.

Captures whatever is playing through the default speakers or headset
(i.e., the meeting audio from Teams/Zoom/etc.) and yields PCM chunks
suitable for speech recognition.

Windows-only — requires PyAudioWPatch for WASAPI loopback support.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncIterator

import numpy as np

logger = logging.getLogger(__name__)

# Target format for speech recognition
TARGET_RATE = 16_000
TARGET_CHANNELS = 1


class AudioCapture:
    """Capture system audio via WASAPI loopback and yield chunked PCM data.

    Usage::

        capture = AudioCapture(chunk_duration=5.0)
        async for chunk in capture.start():
            # chunk is np.ndarray, float32, 16kHz, mono
            lines = await recogniser.transcribe_chunk(chunk)
    """

    def __init__(
        self,
        device_index: int | None = None,
        chunk_duration: float = 5.0,
        silence_threshold: float = 0.002,
    ):
        self.device_index = device_index
        self.chunk_duration = chunk_duration
        self.silence_threshold = silence_threshold
        self.is_capturing = False

        self._queue: asyncio.Queue[np.ndarray | None] = asyncio.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Resolved at start time
        self._source_rate: int = 0
        self._source_channels: int = 0

    async def start(self) -> AsyncIterator[np.ndarray]:
        """Begin capturing and yield audio chunks (float32, 16kHz, mono).

        Each yielded array is approximately ``chunk_duration`` seconds long.
        Silent chunks (RMS below ``silence_threshold``) are skipped.
        """
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        try:
            device = self._resolve_device(pa)
            self._source_rate = int(device["defaultSampleRate"])
            self._source_channels = device["maxInputChannels"]

            logger.info(
                "Audio capture: %s (rate=%d, ch=%d)",
                device["name"],
                self._source_rate,
                self._source_channels,
            )

            self.is_capturing = True
            self._loop = asyncio.get_running_loop()

            # Start capture thread
            self._thread = threading.Thread(
                target=self._capture_thread,
                args=(pa, device),
                daemon=True,
            )
            self._thread.start()

            # Yield chunks from the async queue
            while self.is_capturing:
                try:
                    chunk = await asyncio.wait_for(self._queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if chunk is None:
                    break
                yield chunk

        finally:
            self.is_capturing = False
            # Wait for capture thread to close its stream before
            # terminating PyAudio — avoids C-level crash.
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)
            pa.terminate()

    def stop(self):
        """Signal the capture to stop."""
        self.is_capturing = False
        # Push sentinel to unblock the async generator
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        logger.info("Audio capture stopped.")

    def list_devices(self) -> list[dict]:
        """List available WASAPI loopback devices (for diagnostics)."""
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        devices = []
        try:
            for i in range(pa.get_device_count()):
                dev = pa.get_device_info_by_index(i)
                if dev.get("isLoopbackDevice"):
                    devices.append({
                        "index": i,
                        "name": dev["name"],
                        "channels": dev["maxInputChannels"],
                        "rate": int(dev["defaultSampleRate"]),
                    })
        finally:
            pa.terminate()
        return devices

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_device(self, pa) -> dict:
        """Find the loopback device to capture from."""
        import pyaudiowpatch as pyaudio

        if self.device_index is not None:
            dev = pa.get_device_info_by_index(self.device_index)
            if not dev.get("isLoopbackDevice"):
                raise RuntimeError(
                    f"Device {self.device_index} ({dev['name']}) is not a loopback device."
                )
            return dev

        # Auto-detect: find loopback for the default WASAPI output
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        default_name_prefix = default_out["name"].split("(")[0].strip()

        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice") and dev["name"].startswith(default_name_prefix):
                return dev

        # Fallback: use any loopback device
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice"):
                logger.warning(
                    "Could not match default output; falling back to: %s",
                    dev["name"],
                )
                return dev

        available = self.list_devices()
        raise RuntimeError(
            f"No WASAPI loopback devices found. Available devices: {available}"
        )

    def _capture_thread(self, pa, device: dict):
        """Run in a background thread — reads audio and pushes chunks to the async queue."""
        import pyaudiowpatch as pyaudio

        frames_per_chunk = int(self._source_rate * self.chunk_duration)
        buffer_size = 1024
        buffer: list[bytes] = []
        frames_collected = 0

        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=self._source_channels,
                rate=self._source_rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=buffer_size,
            )

            while self.is_capturing:
                try:
                    data = stream.read(buffer_size, exception_on_overflow=False)
                except OSError:
                    logger.warning("Audio read error, retrying...")
                    continue

                buffer.append(data)
                frames_collected += buffer_size

                if frames_collected >= frames_per_chunk:
                    chunk = self._process_buffer(buffer)
                    buffer.clear()
                    frames_collected = 0

                    if chunk is not None:
                        # Push to async queue from the thread
                        if self._loop and self._loop.is_running():
                            self._loop.call_soon_threadsafe(
                                self._queue.put_nowait, chunk
                            )

        except Exception:
            logger.exception("Audio capture thread error")
        finally:
            if stream:
                stream.stop_stream()
                stream.close()
            # Push sentinel
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait, None
                )

    def _process_buffer(self, raw_frames: list[bytes]) -> np.ndarray | None:
        """Convert raw audio buffer to float32 16kHz mono. Returns None if silent."""
        # Combine raw int16 frames
        raw = b"".join(raw_frames)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        # Downmix to mono if stereo
        if self._source_channels > 1:
            audio = audio.reshape(-1, self._source_channels).mean(axis=1)

        # Resample to 16kHz if needed
        if self._source_rate != TARGET_RATE:
            num_samples = int(len(audio) * TARGET_RATE / self._source_rate)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, num_samples),
                np.arange(len(audio)),
                audio,
            ).astype(np.float32)

        # Silence detection
        rms = np.sqrt(np.mean(audio ** 2))
        if rms < self.silence_threshold:
            logger.debug("Skipping silent chunk (RMS=%.4f)", rms)
            return None

        return audio
