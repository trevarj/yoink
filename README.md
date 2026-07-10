# yoink

A terminal music browser that builds a local, tagged music library. Browse
[MusicBrainz](https://musicbrainz.org), queue full albums, and a background
worker crawls YouTube Music, downloads each track with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), tags it, and writes a clean
`Artist/Album/NN Title.opus` tree. Output is plain files — syncing to a phone or
car (Syncthing, rsync, a Subsonic server) is up to you.

## Features

- **MusicBrainz browse** with a robust cross-field search (`radiohead ok computer`,
  `ok computer - radiohead`, or raw Lucene all work), a description column
  (Live / Compilation / Remix / disambiguation), and an Enter-to-preview
  tracklist with durations.
- **Smart matching** — maps a MusicBrainz release to a YouTube Music album when
  confident, else searches per track. A duration gate plus an exactness-aware
  score reject wrong versions (live/remix/radio-edit); anything below the
  threshold is flagged `needs_review` rather than silently grabbed wrong.
- **Manual resolve** — for a flagged track, open a modal (`m`) to pick from the
  scored candidate list or paste a YouTube URL / videoId.
- **yt-dlp downloads** — best audio extracted to opus, SponsorBlock trims
  non-music intros/outros, cover art embedded, socket timeouts so one bad track
  never wedges the queue. Tracks download concurrently within an album.
- **Tagging** — [beets](https://beets.io) (canonical, isolated library,
  `--search-id`) by default, or direct [mutagen](https://mutagen.readthedocs.io)
  tagging.
- **Resumable queue** — a SQLite job queue survives restarts; the TUI shows live
  status and lets you remove (`x`) or requeue failed tracks (`R`).

## Requirements

Built for [GNU Guix](https://guix.gnu.org): `manifest.scm` provides the binaries
(`uv`, `python`, `ffmpeg`, `ruff`, `git`) and [uv](https://docs.astral.sh/uv/)
manages the Python dependencies from `pyproject.toml`.

> Note: uv must use the Guix-provided CPython — its downloadable builds are FHS
> binaries that won't run on Guix System. `.envrc` sets `UV_PYTHON_DOWNLOADS=never`.

## Setup

With [direnv](https://direnv.net):

```sh
direnv allow      # loads the guix shell + creates the uv venv
uv sync           # install dependencies
```

Or manually:

```sh
guix shell -m manifest.scm -- uv sync
```

## Usage

```sh
yoink --write-config    # writes config.toml; set mb_contact to a real email
yoink                   # launch the TUI
```

Keys: `/` search · `Enter` preview tracklist · `a` queue album · `1` discover ·
`2` queue · `x` remove · `R` requeue failed · `m` resolve a flagged track ·
`r` refresh · `q` quit.

## Configuration

`$XDG_CONFIG_HOME/yoink/config.toml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `mb_contact` | — | Contact (email/URL) for the MusicBrainz User-Agent. Required by MB etiquette. |
| `tagger` | `beets` | `beets` (canonical import) or `mutagen` (direct). |
| `audio_codec` | `opus` | Codec yt-dlp extracts to. |
| `download_concurrency` | `3` | Parallel track downloads per album. Lower if throttled. |
| `duration_gate_s` / `duration_soft_s` | `3` / `7` | Match duration tolerance vs the MusicBrainz track length. |
| `min_match_score` | `6.0` | Below this, a track is flagged `needs_review`. |
| `min_audio_bitrate` | `128.0` | Minimum kbps for an automatic match; `0` disables the quality probe. |
| `strip_featured_artists` | `true` | Remove featured guests from track artist tags for consistent album grouping. |
| `replaygain` | `true` | Write non-destructive R128 track and integrated album loudness tags. |
| `music_dir` | `$XDG_MUSIC_DIR` or `~/Music` | Library output root. |
| `state_dir` | `$XDG_STATE_HOME/yoink` | Durable queue, staging, beets library, and downloader archive root. |
| `cache_dir` | `$XDG_CACHE_HOME/yoink` | Disposable MusicBrainz response and cover-art cache root. |

All values are validated at startup; unknown keys and invalid types or ranges
are reported as configuration errors.

## Development

```sh
uv run pytest tests/test_*.py     # pure unit tests (no network)
guix shell ruff -- ruff check src tests
```

`tests/smoke_*.py` are live integration checks (they hit MusicBrainz / YouTube)
and need ffmpeg on PATH.

## Responsible use

yoink downloads from sources you point it at via yt-dlp. Only download content
you are entitled to — your own uploads, public-domain or Creative-Commons
material, or anything you have rights to. Respecting source terms of service and
copyright is your responsibility.

## License

MIT
