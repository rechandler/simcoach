import yaml

from simcoach.config import load_config
from simcoach.setup_wizard import run_setup


def scripted(*answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_setup_writes_config_and_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "already-set")
    monkeypatch.delenv("GARAGE61_TOKEN", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"units": "imperial", "voice": {"tts": "piper"}}))
    # keyboard key, toggle mode, Claude, skip Garage 61
    run_setup(path, ask=scripted("f14", "2", "1", "n"), hardware=False)
    cfg = load_config(path)
    assert cfg.voice.ptt == "keyboard" and cfg.voice.keyboard_key == "f14"
    assert cfg.voice.ptt_mode == "toggle"
    assert cfg.llm.provider == "anthropic"
    assert cfg.units == "imperial" and cfg.voice.tts == "piper"  # untouched


def test_setup_ollama_choice(tmp_path, monkeypatch):
    monkeypatch.setenv("GARAGE61_TOKEN", "x")
    path = tmp_path / "config.yaml"
    run_setup(path, ask=scripted("", "", "2", "llama3.1:8b"), hardware=False)
    cfg = load_config(path)
    assert cfg.llm.provider == "ollama" and cfg.llm.ollama_model == "llama3.1:8b"
    assert cfg.voice.keyboard_key == "f13" and cfg.voice.ptt_mode == "hold"
