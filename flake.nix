{
  description = "Yoink development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };

      python = pkgs.python312.override {
        self = python;
        packageOverrides = _final: prev: {
          # curl-cffi (yt-dlp's impersonation backend) runs its test suite
          # against litestar/fastapi/pint, which drag scipy and friends in as
          # uncached source builds. Yoink only needs the library.
          curl-cffi = prev.curl-cffi.overridePythonAttrs { doCheck = false; };
        };
      };

      # Yoink writes its own beets config enabling exactly three plugins, so the
      # default nixpkgs plugin set (numba, imagemagick, nodejs, ...) is dead
      # weight. Its test suite is skipped for the same reason.
      beets = python.pkgs.beets.override {
        disableAllPlugins = true;
        pluginOverrides = {
          musicbrainz.enable = true;
          fromfilename.enable = true;
          replaygain.enable = true;
        };
        doCheck = false;
      };

      # Tools Yoink shells out to at runtime: ffmpeg for ReplayGain scanning and
      # yt-dlp postprocessing, `beet` for the canonical library import.
      runtimeTools = [
        pkgs.ffmpeg
        beets
        python.pkgs.yt-dlp
      ];

      yoink = python.pkgs.buildPythonApplication {
        pname = "yoink";
        version = "0.2.3";
        pyproject = true;

        src = ./.;

        build-system = [ python.pkgs.hatchling ];

        dependencies = [
          beets
        ]
        ++ (with python.pkgs; [
          httpx
          musicbrainzngs
          mutagen
          platformdirs
          rapidfuzz
          textual
          yt-dlp
          ytmusicapi
        ]);

        nativeBuildInputs = [ pkgs.makeWrapper ];

        # The tests are run from the development shell against the uv venv; the
        # build only checks that the entry point imports.
        doCheck = false;
        pythonImportsCheck = [ "yoink.cli" ];

        postFixup = ''
          wrapProgram $out/bin/yoink \
            --prefix PATH : ${pkgs.lib.makeBinPath runtimeTools}
        '';

        meta = {
          description = "TUI music browser that crawls YouTube Music and yoinks full albums via yt-dlp";
          mainProgram = "yoink";
        };
      };
    in
    {
      packages.${system} = {
        default = yoink;
        inherit yoink;
      };

      # `nix run` / `nix run github:...#yoink` launch the TUI.
      apps.${system}.default = {
        type = "app";
        program = "${yoink}/bin/yoink";
        meta = { inherit (yoink.meta) description; };
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          ffmpeg
          git
          python312
          ruff
          uv
        ];

        UV_PYTHON_DOWNLOADS = "never";

        shellHook = ''
          if [ ! -d .venv ]; then
            uv venv --python "$(command -v python3)"
          fi
          source .venv/bin/activate
        '';
      };

      formatter.${system} = pkgs.nixfmt;
    };
}
