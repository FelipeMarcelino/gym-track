{
  pkgs,
  lib,
  stdenv,
  ...
}:
let
  pkgs-unfree = import pkgs.path {
    inherit (pkgs) system;
    config.allowUnfree = true;
  };
  pythonPackages = pkgs-unfree.python313Packages;
in
pkgs-unfree.mkShell {
  buildInputs = [
    pythonPackages.python
    pythonPackages.venvShellHook
    pkgs-unfree.autoPatchelfHook

    # Python dependency management (WS-1, decision D1).
    pkgs-unfree.uv

    # `make` drives fmt/lint/typecheck/test.
    pkgs-unfree.gnumake

    # Docker CLI + compose plugin for local infrastructure and Testcontainers.
    # NOTE: this provides the *client* only. The dockerd daemon is a host-level
    # service (systemd) and cannot be supplied by a user devshell.
    pkgs-unfree.docker-client
    pkgs-unfree.docker-compose

  ];
  venvDir = "./.venv";
  postVenvCreation = ''

    unset SOURCE_DATE_EPOCH
    autoPatchelf ./.venv   '';
  postShellHook = ''

    unset SOURCE_DATE_EPOCH
    export LD_LIBRARY_PATH=${lib.makeLibraryPath [ stdenv.cc.cc ]}:$LD_LIBRARY_PATH

    # Point uv at the venv venvShellHook already created, instead of letting it
    # build its own, and forbid it from downloading a Python that is not the one
    # Nix pinned above.
    export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
    export UV_PYTHON_DOWNLOADS=never
    export UV_PYTHON="${pythonPackages.python}/bin/python3.13" '';
}
