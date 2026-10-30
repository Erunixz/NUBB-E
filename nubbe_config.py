"""Loads config.toml (next to this file) for all NUBB-E scripts.

    from nubbe_config import CFG, path
    CFG["brain"]["port"]          # -> 8080
    path(CFG["brain"]["model"])   # -> absolute Path, relative to the project folder

Any setting missing from config.toml (or the whole file) falls back to the
defaults below, so the scripts still run without it. Standard library only.
"""
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
EXE = ".exe" if sys.platform == "win32" else ""

DEFAULTS = {
    "brain": {
        "llama_server": f"bin/llama.cpp/llama-server{EXE}",
        "model": "models/Qwen3.6-35B-A3B-MTP-UD-IQ4_XS.gguf",
        "mmproj": "models/mmproj-Qwen3.6-35B-A3B-F16.gguf",
        "n_cpu_moe": 31,
        "threads": 16,
        "context": 32768,
        "extra_flags": ["-ngl", "99", "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--jinja"],
        "port": 8080,
        "reasoning_budget": 600,
        "idle_unload_minutes": 10,
    },
    "app": {
        "port": 8765,
        "history_turns": 40,
        "quick_bar": True,
        "quick_hotkey": "alt+space",
        "close_to_background": True,
    },
    "voice": {
        "whisper_model": "base.en",
        "kokoro_voice": "af_heart",
        "kokoro_speed": 1.0,
        "piper_voice": "",
        "mic_threshold": 0.015,
        "barge_in": True,
        "say": {"Erfan": "Er-fawn", "NUBB-E": "Noobee"},
    },
    "tools": {
        "workspace_dir": "*",
    },
    "web": {
        "web_enabled": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(file: Path = CONFIG_PATH) -> dict:
    if not file.exists():
        return _merge(DEFAULTS, {})
    try:
        with open(file, "rb") as f:
            return _merge(DEFAULTS, tomllib.load(f))
    except tomllib.TOMLDecodeError as e:
        print(f"config.toml has a mistake ({e}). Using the default settings.", file=sys.stderr)
        return _merge(DEFAULTS, {})


def path(p: str) -> Path:
    """Relative paths in config.toml are relative to the project folder."""
    return Path(p) if Path(p).is_absolute() else HERE / p


def llama_args(brain: dict) -> list:
    """The llama-server command line (without the exe) built from the [brain] settings."""
    args = ["-m", str(path(brain["model"]))]
    if brain.get("mmproj"):
        args += ["--mmproj", str(path(brain["mmproj"]))]
    if brain.get("n_cpu_moe"):
        args += ["--n-cpu-moe", str(brain["n_cpu_moe"])]
    if brain.get("threads"):
        args += ["-t", str(brain["threads"])]
    if brain.get("reasoning_budget", -1) >= 0:
        args += ["--reasoning-budget", str(brain["reasoning_budget"]),
                 "--reasoning-budget-message", "Okay, I've thought enough. Time to answer."]
    args += ["-c", str(brain["context"]), *brain["extra_flags"], "--port", str(brain["port"])]
    return args


CFG = load()
