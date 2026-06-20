;; Dev environment for yoink.
;;
;; Guix supplies the binaries; uv manages the Python dependency tree from
;; pyproject.toml (see .envrc / uv.lock). uv must NOT download its own CPython
;; -- its prebuilt FHS binaries don't run on Guix System -- so we hand it the
;; Guix `python` here and set UV_PYTHON_DOWNLOADS=never in .envrc.
(specifications->manifest
 (list "uv"          ; Python dependency + venv manager
       "python"      ; the interpreter uv builds the venv against
       "ffmpeg"      ; runtime: yt-dlp audio extraction + beets
       "ruff"        ; linter (PyPI ruff wheel is an FHS binary, unusable here)
       "git"))       ; uv/yt-dlp occasionally shell out to it
