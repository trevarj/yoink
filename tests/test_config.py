from __future__ import annotations

import sys
from pathlib import Path

import pytest

from yoink import cli
from yoink import config as configmod
from yoink.config import Config


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"duration_gate_s": -1}, "duration_gate_s"),
        ({"duration_gate_s": 8, "duration_soft_s": 7}, "duration_soft_s"),
        ({"download_concurrency": 0}, "download_concurrency"),
        ({"download_concurrency": 1.5}, "download_concurrency"),
        ({"audio_codec": ""}, "audio_codec"),
        ({"min_audio_bitrate": -1}, "min_audio_bitrate"),
        ({"strip_featured_artists": "yes"}, "strip_featured_artists"),
        ({"replaygain": 1}, "replaygain"),
        ({"tagger": "other"}, "tagger"),
        ({"tagger": ["beets"]}, "tagger"),
    ],
)
def test_config_rejects_invalid_values(overrides, message):
    with pytest.raises(ValueError, match=message):
        Config(**overrides)


def test_load_config_rejects_unknown_keys(monkeypatch, tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('mystery_option = true\n')
    monkeypatch.setattr(configmod, "config_path", lambda: path)
    with pytest.raises(ValueError, match="unknown configuration key.*mystery_option"):
        configmod.load_config()


def test_load_config_requires_path_strings(monkeypatch, tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("state_dir = 12\n")
    monkeypatch.setattr(configmod, "config_path", lambda: path)
    with pytest.raises(ValueError, match="state_dir must be a non-empty path string"):
        configmod.load_config()


def test_sample_config_covers_operational_settings():
    for key in (
        "min_audio_bitrate",
        "strip_featured_artists",
        "replaygain",
        "state_dir",
        "cache_dir",
    ):
        assert key in cli._SAMPLE_CONFIG


def test_cli_reports_invalid_config_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["yoink"])

    def invalid_config():
        raise ValueError("bad key")

    monkeypatch.setattr(cli, "load_config", invalid_config)
    with pytest.raises(SystemExit, match="2"):
        cli.main()
    assert "invalid configuration: bad key" in capsys.readouterr().err
