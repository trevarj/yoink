{
  description = "Yoink development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
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
