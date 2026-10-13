"""Interactive first-time setup: ``simcoach setup``.

Finds the push-to-talk button by asking you to press it, picks a microphone,
chooses the coach AI (Claude, local Ollama or none), stores secrets as user
environment variables, downloads the speech model and writes
~/.simcoach/config.yaml. Safe to re-run; existing answers become defaults.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

import yaml

from simcoach.config import LLMConfig, default_data_dir

Ask = Callable[[str], str]


def _ask(prompt: str, default: str = "", ask: Ask = input) -> str:
    suffix = f" [{default}]" if default else ""
    ans = ask(f"{prompt}{suffix}: ").strip()
    return ans or default


def _yes(prompt: str, default: bool = True, ask: Ask = input) -> bool:
    d = "Y/n" if default else "y/N"
    ans = ask(f"{prompt} [{d}]: ").strip().lower()
    return default if not ans else ans.startswith("y")


def _choose(prompt: str, options: list[str], default: int = 1, ask: Ask = input) -> int:
    print(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        ans = _ask("Choose", str(default), ask)
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans)


def set_user_env(name: str, value: str) -> bool:
    """Persist an environment variable for the current user (Windows: registry
    via setx; elsewhere we print the export line)."""
    os.environ[name] = value
    if sys.platform == "win32":
        r = subprocess.run(["setx", name, value], capture_output=True, text=True)
        return r.returncode == 0
    print(f"  Add this to your shell profile:  export {name}='{value}'")
    return False


def detect_button(timeout_s: float = 30.0) -> tuple[str, int] | None:
    """Wait for any joystick button press; return (device name, button)."""
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    pygame.init()
    pygame.joystick.init()
    joys = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    if not joys:
        return None
    for j in joys:
        print(f"  found: {j.get_name()}")
    pygame.event.clear()
    end = time.time() + timeout_s
    while time.time() < end:
        for ev in pygame.event.get():
            if ev.type == pygame.JOYBUTTONDOWN:
                j = next((x for x in joys if x.get_instance_id() == ev.instance_id), None)
                if j is not None:
                    return j.get_name(), int(ev.button)
        time.sleep(0.01)
    return None


def list_microphones() -> list[str]:
    import sounddevice as sd

    seen, out = set(), []
    default_api = sd.default.hostapi if isinstance(sd.default.hostapi, int) else 0
    for d in sd.query_devices():
        # One entry per physical mic: prefer the default host API (MME on Windows).
        if d["max_input_channels"] > 0 and d["hostapi"] == default_api and d["name"] not in seen:
            seen.add(d["name"])
            out.append(d["name"])
    return out


def ollama_running(url: str) -> list[str] | None:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


def merge_config(path: Path, updates: dict) -> dict:
    data: dict = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for section, values in updates.items():
        if isinstance(values, dict):
            data.setdefault(section, {}).update(values)
        else:
            data[section] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Written by `simcoach setup`. See config.example.yaml for every option.\n"
                    + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return data


def run_setup(config_path: Path | None = None, ask: Ask = input, hardware: bool = True) -> Path:
    path = config_path or default_data_dir() / "config.yaml"
    existing = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    existing = existing or {}
    voice: dict = {}
    llm: dict = {}
    updates: dict = {"voice": voice, "llm": llm}

    print("\n=== simcoach setup ===\n")

    # 1. Push-to-talk ----------------------------------------------------
    print("1) Push-to-talk")
    found = None
    if hardware and _yes("Use a button on your wheel / button box?", True, ask):
        print("  Press and release the button you want to use (30 s)...")
        try:
            found = detect_button()
        except Exception as e:  # pygame missing or no SDL
            print(f"  Could not read controllers: {e}")
        if found:
            print(f"  Got it: {found[0]} button {found[1]}")
            voice.update(ptt="joystick", joystick_name=found[0], joystick_button=found[1])
        else:
            print("  No button press detected.")
    if not found:
        key = _ask("  Keyboard key to hold for push-to-talk (map a wheel button to it in SimHub/JoyToKey)",
                   existing.get("voice", {}).get("keyboard_key", "f13"), ask)
        voice.update(ptt="keyboard", keyboard_key=key)
    mode = _choose("  Talk mode:", ["hold the button while talking", "press once to start, again to stop"], 1, ask)
    voice["ptt_mode"] = "hold" if mode == 1 else "toggle"

    # 2. Microphone ------------------------------------------------------
    print("\n2) Microphone")
    mics = []
    if hardware:
        try:
            mics = list_microphones()
        except Exception as e:
            print(f"  Could not list microphones: {e}")
    if mics:
        idx = _choose("  Which microphone?", ["System default", *mics], 1, ask)
        voice["input_device"] = None if idx == 1 else mics[idx - 2]
    else:
        print("  Using the system default microphone.")

    # 3. Coach AI --------------------------------------------------------
    print("\n3) Coach AI (answers your questions; automatic callouts don't need it)")
    choice = _choose("  Which AI?", [
        "Claude (cloud) - best answers, no load on your PC (needs an Anthropic API key)",
        "Local via Ollama - free/offline, uses your GPU alongside iRacing",
        "None - rule-based answers only",
    ], 1, ask)
    if choice == 1:
        llm["provider"] = "anthropic"
        if os.environ.get("ANTHROPIC_API_KEY"):
            print("  ANTHROPIC_API_KEY is already set.")
        else:
            print("  Create a key at https://console.anthropic.com/settings/keys")
            key = getpass.getpass("  Paste your API key (hidden, Enter to skip): ").strip() if ask is input \
                else ask("key: ").strip()
            if key:
                set_user_env("ANTHROPIC_API_KEY", key)
                print("  Saved as a user environment variable.")
            else:
                print("  Skipped - set ANTHROPIC_API_KEY later, or questions get rule-based answers.")
    elif choice == 2:
        llm["provider"] = "ollama"
        model = _ask("  Ollama model", existing.get("llm", {}).get("ollama_model", LLMConfig.ollama_model), ask)
        llm["ollama_model"] = model
        models = ollama_running(LLMConfig.ollama_url) if hardware else None
        if models is None and hardware and sys.platform == "win32" and not shutil.which("ollama") \
                and shutil.which("winget") and _yes("  Ollama isn't installed. Install it now with winget?", True, ask):
            subprocess.run(["winget", "install", "--exact", "--id", "Ollama.Ollama",
                            "--accept-package-agreements", "--accept-source-agreements"])
            print("  Installed. Start Ollama, then run:  ollama pull " + model)
        elif models is None:
            print("  Ollama isn't running. Install it from https://ollama.com, start it, then run:")
            print(f"  ollama pull {model}")
        elif not any(m.startswith(model) for m in models):
            exe = shutil.which("ollama")
            if exe and _yes(f"  Download {model} now (several GB)?", True, ask):
                subprocess.run([exe, "pull", model])
    else:
        llm["provider"] = "none"

    # 4. Garage 61 -------------------------------------------------------
    print("\n4) Garage 61 (optional - import reference laps from you or teammates)")
    if not os.environ.get("GARAGE61_TOKEN") and _yes("  Set up a Garage 61 token now?", False, ask):
        print("  Create one at https://garage61.net/developer (scope: driving_data)")
        tok = ask("  Paste token: ").strip()
        if tok:
            set_user_env("GARAGE61_TOKEN", tok)

    merge_config(path, updates)
    print(f"\nSaved {path}")

    # 5. Downloads & voice check ----------------------------------------
    if hardware:
        print("\n5) Downloading the speech recognition model (first time only)...")
        try:
            from simcoach.config import load_config
            from simcoach.voice.stt import Transcriber

            cfg = load_config(path)
            Transcriber(cfg.voice.stt_model, cfg.voice.stt_device, cfg.voice.stt_compute_type)
            print("  Done.")
        except Exception as e:
            print(f"  Skipped ({e}). It will download on first run.")
        try:
            from simcoach.config import load_config
            from simcoach.voice.tts import make_speaker

            sp = make_speaker(load_config(path).voice)
            sp.say("Radio check. Coach is ready when you are.", 0)
            sp.wait_idle(15)
        except Exception as e:
            print(f"  Voice test failed: {e}")

    print("\nAll set. Start iRacing, then run:  simcoach run   (or the SimCoach desktop shortcut)")
    print("Try it without iRacing first:        simcoach demo\n")
    return path
