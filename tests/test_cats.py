"""cats.json schema + loader smoke tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_rc.service import _load_cats  # noqa: SLF001 — module-private helper

CATS_PATH = Path(__file__).parent.parent / "src" / "codex_rc" / "assets" / "cats.json"


def test_cats_file_exists() -> None:
    assert CATS_PATH.is_file()


def test_cats_file_is_valid_json() -> None:
    data = json.loads(CATS_PATH.read_text())
    assert isinstance(data, dict) and "cats" in data
    assert isinstance(data["cats"], list)


@pytest.mark.parametrize("cat", json.loads(CATS_PATH.read_text())["cats"])
def test_cat_shape(cat: dict) -> None:
    assert isinstance(cat.get("name"), str) and cat["name"]
    frames = cat.get("frames")
    assert isinstance(frames, list) and frames
    assert all(isinstance(f, str) and f for f in frames)
    assert len(frames) >= 2, "need at least 2 frames for animation"


def test_at_least_five_cats() -> None:
    data = json.loads(CATS_PATH.read_text())
    assert len(data["cats"]) >= 5


def test_load_cats_default_path() -> None:
    cats = _load_cats(None)
    assert len(cats) >= 5


def test_load_cats_missing_file(tmp_path: Path) -> None:
    cats = _load_cats(tmp_path / "does-not-exist.json")
    assert cats == []
