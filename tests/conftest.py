"""Shared safety net for every test: nothing reads or rings Erfan's real windows through UI Automation.
Tests that check UI Automation for real use their own off-screen window and the `own_window` marker."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nubbe_uia  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "own_window: uses a real off-screen window the test made itself")


@pytest.fixture(autouse=True)
def no_real_windows(request, monkeypatch):
    if request.node.get_closest_marker("own_window"):
        return
    monkeypatch.setattr(nubbe_uia, "front_window", lambda: (0, ""))
    monkeypatch.setattr(nubbe_uia, "read", lambda hwnd, limit=0: [])
    monkeypatch.setattr(nubbe_uia, "ring", lambda rect, seconds=4.0, wait=False: True)
