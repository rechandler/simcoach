"""Configuration loading.

Everything has a sensible default so ``simcoach run`` works with no config
file at all. Users override values in a YAML file (see config.example.yaml).
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def default_data_dir() -> Path:
    env = os.environ.get("SIMCOACH_HOME")
    if env:
        return Path(env)
    return Path.home() / ".simcoach"


@dataclass
class CoachingConfig:
    # Only coach a corner when it cost at least this much time vs the reference.
    min_time_loss_s: float = 0.06
    # Brake point thresholds (metres) before we call it "late" or "early".
    late_brake_m: float = 8.0
    early_brake_m: float = 12.0
    # Minimum-speed gap (km/h) worth mentioning.
    min_speed_kph: float = 4.0
    # Throttle pickup gap (metres) worth mentioning.
    throttle_late_m: float = 15.0
    # Extra coasting distance (metres) worth mentioning.
    coast_m: float = 15.0
    # Peak brake pressure gap (0-1) worth mentioning.
    brake_pressure: float = 0.12
    # Exit speed gap (km/h) worth mentioning.
    exit_speed_kph: float = 4.0
    # Say something positive when a corner gains at least this much.
    praise_gain_s: float = 0.12
    praise_enabled: bool = True


@dataclass
class CalloutConfig:
    # Minimum gap between two spoken callouts.
    min_gap_s: float = 6.0
    # Drop a queued callout if it could not be delivered within this time.
    max_age_s: float = 10.0
    # Don't repeat the same advice for the same corner within N laps.
    repeat_laps: int = 2
    # Don't talk this close (metres) to the next braking zone.
    quiet_before_brake_m: float = 120.0
    # Short reminder before the corner on the following lap.
    pre_corner_reminders: bool = False
    reminder_lead_s: float = 3.0
    # Global switch for automatic callouts (questions still work when off).
    enabled: bool = True


@dataclass
class LLMConfig:
    # "anthropic" (Claude, cloud) or "ollama" (local) or "none".
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    # Claude: thinking effort. "low" keeps spoken answers fast.
    effort: str = "low"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    max_tokens: int = 1024
    timeout_s: float = 20.0


@dataclass
class VoiceConfig:
    # Push-to-talk source: "joystick" (wheel button), "keyboard", or "none".
    ptt: str = "joystick"
    # Joystick: substring of the device name (e.g. "Fanatec", "Simucube",
    # "MOZA") and the 0-based button index. Use `simcoach devices` to find them.
    joystick_name: str = ""
    joystick_button: int = 0
    keyboard_key: str = "f13"
    # "hold" = talk while held; "toggle" = press once to start, again to stop.
    ptt_mode: str = "hold"
    # Speech-to-text (faster-whisper). CPU keeps the GPU free for iRacing.
    stt_model: str = "small.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    input_device: str | None = None
    # Text-to-speech: "sapi" (Windows built-in via pyttsx3), "piper", "console".
    tts: str = "sapi"
    tts_rate: int = 185
    tts_voice: str = ""
    piper_exe: str = "piper"
    piper_model: str = ""


@dataclass
class OverlayConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass
class Garage61Config:
    # Personal access token from https://garage61.net/developer
    token: str = ""


@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    coaching: CoachingConfig = field(default_factory=CoachingConfig)
    callouts: CalloutConfig = field(default_factory=CalloutConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    garage61: Garage61Config = field(default_factory=Garage61Config)
    # "metric" or "imperial" - affects spoken distances/speeds.
    units: str = "metric"


def _merge(dc: Any, values: dict[str, Any]) -> Any:
    for f in dataclasses.fields(dc):
        if f.name not in values:
            continue
        current = getattr(dc, f.name)
        new = values[f.name]
        if dataclasses.is_dataclass(current) and isinstance(new, dict):
            _merge(current, new)
        elif isinstance(current, Path):
            setattr(dc, f.name, Path(os.path.expanduser(str(new))))
        else:
            setattr(dc, f.name, new)
    return dc


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    candidates = [Path(path)] if path else [Path("simcoach.yaml"), default_data_dir() / "config.yaml"]
    for p in candidates:
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                _merge(cfg, yaml.safe_load(fh) or {})
            break
    else:
        if path:
            raise FileNotFoundError(path)
    if not cfg.garage61.token:
        cfg.garage61.token = os.environ.get("GARAGE61_TOKEN", "")
    return cfg
