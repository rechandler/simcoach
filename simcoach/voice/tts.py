"""Text-to-speech with a priority queue.

Speech runs on its own thread so telemetry processing never blocks. Higher
priority messages (answers to questions) jump ahead of queued coaching
callouts, and stale callouts are dropped instead of being read out late.
"""

from __future__ import annotations

import itertools
import logging
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from simcoach.coach.callouts import PRIO_ANSWER, PRIO_SYSTEM
from simcoach.config import VoiceConfig

log = logging.getLogger(__name__)


class Speaker:
    """Base class: queues messages and plays them on a worker thread."""

    max_age_s = 8.0

    def __init__(self):
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._muted = threading.Event()
        self.speaking = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()

    def say(self, text: str, priority: int = 2) -> None:
        self._q.put((priority, next(self._seq), time.monotonic(), text))

    __call__ = say

    def interrupt(self) -> None:
        """Drop queued coaching (e.g. the driver pressed push-to-talk)."""
        kept = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item[0] <= PRIO_ANSWER:
                kept.append(item)
        for item in kept:
            self._q.put(item)
        self.stop_current()

    def stop_current(self) -> None:
        pass

    def _run(self) -> None:
        self._init_thread()
        while True:
            prio, _, created, text = self._q.get()
            if prio > PRIO_SYSTEM and prio != PRIO_ANSWER and time.monotonic() - created > self.max_age_s:
                continue
            self.speaking.set()
            try:
                self._speak(text)
            except Exception:
                log.exception("TTS failed")
            finally:
                self.speaking.clear()

    def wait_idle(self, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        while (not self._q.empty() or self.speaking.is_set()) and time.monotonic() < end:
            time.sleep(0.05)

    def _init_thread(self) -> None:
        pass

    def _speak(self, text: str) -> None:
        raise NotImplementedError


class ConsoleSpeaker(Speaker):
    def _speak(self, text: str) -> None:
        print(f"[COACH] {text}", flush=True)


class SapiSpeaker(Speaker):
    """Windows built-in voices via pyttsx3 (also works with espeak/NSSpeech)."""

    def __init__(self, rate: int = 185, voice: str = ""):
        import pyttsx3  # noqa: F401  - fail early (in make_speaker) if missing

        self.rate, self.voice = rate, voice
        super().__init__()

    def _init_thread(self) -> None:
        try:  # pyttsx3's SAPI driver needs COM initialised on this thread
            import pythoncom  # type: ignore

            pythoncom.CoInitialize()
        except ImportError:
            pass
        import pyttsx3

        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", self.rate)
        if self.voice:
            for v in self.engine.getProperty("voices"):
                if self.voice.lower() in (v.name or "").lower():
                    self.engine.setProperty("voice", v.id)
                    break

    def _speak(self, text: str) -> None:
        print(f"[COACH] {text}", flush=True)
        self.engine.say(text)
        self.engine.runAndWait()

    def stop_current(self) -> None:
        eng = getattr(self, "engine", None)
        if eng is not None:
            eng.stop()


class PiperSpeaker(Speaker):
    """Piper neural TTS (https://github.com/rhasspy/piper): local, natural sounding."""

    def __init__(self, exe: str, model: str):
        if not model:
            raise ValueError("voice.piper_model must point to a .onnx voice file")
        self.exe = shutil.which(exe) or exe
        self.model = model
        super().__init__()

    def _speak(self, text: str) -> None:
        import wave

        import numpy as np
        import sounddevice as sd

        print(f"[COACH] {text}", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "out.wav"
            subprocess.run([self.exe, "--model", self.model, "--output_file", str(wav)],
                           input=text.encode("utf-8"), check=True, capture_output=True)
            with wave.open(str(wav), "rb") as w:
                sr = w.getframerate()
                data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        sd.play(data, sr)
        sd.wait()

    def stop_current(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:
            pass


def make_speaker(cfg: VoiceConfig) -> Speaker:
    kind = cfg.tts.lower()
    try:
        if kind == "sapi":
            return SapiSpeaker(cfg.tts_rate, cfg.tts_voice)
        if kind == "piper":
            return PiperSpeaker(cfg.piper_exe, cfg.piper_model)
    except Exception as e:
        log.warning("TTS %r unavailable (%s); printing to console instead", kind, e)
    return ConsoleSpeaker()
