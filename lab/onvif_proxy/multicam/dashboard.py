from __future__ import annotations

from pathlib import Path

_WEB_ROOT = Path(__file__).with_name("web")


def load_index() -> bytes:
    return (_WEB_ROOT / "index.html").read_bytes()


def load_stylesheet() -> bytes:
    return (_WEB_ROOT / "app.css").read_bytes()


def load_script() -> bytes:
    return (_WEB_ROOT / "app.js").read_bytes()
