import tomllib
from pathlib import Path

from yoink import __version__
from yoink.config import Config
from yoink.metadata import musicbrainz as mbmod


def test_musicbrainz_uses_package_version(monkeypatch, tmp_path: Path):
    calls = []
    monkeypatch.setattr(mbmod.musicbrainzngs, "set_useragent", lambda *args: calls.append(args))
    monkeypatch.setattr(mbmod.musicbrainzngs, "set_rate_limit", lambda **_kwargs: None)

    mbmod.MusicBrainz(Config(cache_dir=tmp_path / "cache", mb_contact="me@example.com"))

    assert calls == [("yoink", __version__, "me@example.com")]


def test_runtime_version_matches_project_metadata():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert __version__ == project["project"]["version"]
