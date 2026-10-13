"""Microphone capture and local speech-to-text (faster-whisper)."""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

# Biases Whisper toward racing vocabulary (and corner names, appended at runtime).
BASE_PROMPT = ("Racing coach radio. Turn one, turn two, apex, trail braking, brake point, "
               "throttle, understeer, oversteer, chicane, hairpin, exit, entry, lap time, delta.")


class Recorder:
    """Records from the microphone between start() and stop()."""

    def __init__(self, device: str | int | None = None):
        import sounddevice as sd

        self._sd = sd
        self.device = device
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream = None

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                                device=self.device, callback=self._cb)
            self._stream.start()

    def _cb(self, indata, frames, t, status) -> None:
        self._chunks.append(indata[:, 0].copy())

    def stop(self) -> np.ndarray:
        with self._lock:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
            self._chunks = []
        return audio


class Transcriber:
    def __init__(self, model: str = "small.en", device: str = "cpu", compute_type: str = "int8"):
        from faster_whisper import WhisperModel

        log.info("Loading speech model %s on %s...", model, device)
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.hint = BASE_PROMPT

    def set_vocabulary(self, words: list[str]) -> None:
        self.hint = BASE_PROMPT + " " + ", ".join(words)

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < SAMPLE_RATE * 0.3:  # under 0.3 s: accidental press
            return ""
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                            initial_prompt=self.hint, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
