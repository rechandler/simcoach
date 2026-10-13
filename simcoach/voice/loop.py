"""Glue: push-to-talk -> record -> transcribe -> ask the coach -> speak."""

from __future__ import annotations

import logging
import threading

from simcoach.coach.assistant import Assistant
from simcoach.config import VoiceConfig
from simcoach.voice.ptt import JoystickPTT, KeyboardPTT, PushToTalk
from simcoach.voice.stt import Recorder, Transcriber
from simcoach.voice.tts import Speaker

log = logging.getLogger(__name__)


class VoiceLoop:
    def __init__(self, cfg: VoiceConfig, assistant: Assistant, speaker: Speaker):
        self.cfg = cfg
        self.assistant = assistant
        self.speaker = speaker
        self.recorder = Recorder(cfg.input_device)
        self.stt = Transcriber(cfg.stt_model, cfg.stt_device, cfg.stt_compute_type)
        self.ptt = self._make_ptt()
        self._busy = threading.Lock()

    def _make_ptt(self) -> PushToTalk:
        kw = dict(on_start=self._start, on_stop=self._stop, mode=self.cfg.ptt_mode)
        if self.cfg.ptt == "keyboard":
            return KeyboardPTT(self.cfg.keyboard_key, **kw)
        return JoystickPTT(self.cfg.joystick_name, self.cfg.joystick_button, **kw)

    def update_vocabulary(self) -> None:
        track = self.assistant.engine.snapshot()["track"]
        if track:
            words = [c.name for c in track.corners] + [a for c in track.corners for a in c.aliases]
            self.stt.set_vocabulary(words)

    def start(self) -> None:
        self.ptt.start()
        log.info("Voice ready (%s push-to-talk, %s mode)", self.cfg.ptt, self.cfg.ptt_mode)

    def _start(self) -> None:
        self.speaker.interrupt()  # stop talking over the driver
        try:
            self.recorder.start()
        except Exception:
            log.exception("could not open microphone")

    def _stop(self) -> None:
        audio = self.recorder.stop()
        threading.Thread(target=self._handle, args=(audio,), daemon=True).start()

    def _handle(self, audio) -> None:
        if not self._busy.acquire(blocking=False):
            return  # still answering the previous question
        try:
            self.update_vocabulary()
            text = self.stt.transcribe(audio)
            if not text:
                return
            self.assistant.ask(text)
        except Exception:
            log.exception("voice handling failed")
        finally:
            self._busy.release()
