{
  description = "Simple Python development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      system = "x86_64-linux";

      pkgs = import nixpkgs {
        inherit system;
      };

      python = pkgs.python313.withPackages (
        ps: with ps; [
          numpy
          pillow
          scipy
          matplotlib
          pandas
          ipython
          psutil
          pytest
          requests
          rerun-sdk
        ]
      );
    in
    {
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python

          uv
          ruff
          pyright
          typst

          pkg-config
          gcc
        ];

        shellHook = ''
          export PYTHONNOUSERSITE=1

          export UV_PYTHON=${python}/bin/python
          export UV_PYTHON_DOWNLOADS=never
          export UV_PROJECT_ENVIRONMENT="$PWD/.venv"

          echo "Python: $(python --version)"
        '';
      };
    };
}
