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

  ];
  venvDir = "./.venv";
  postVenvCreation = ''

    unset SOURCE_DATE_EPOCH    
    autoPatchelf ./.venv   '';
  postShellHook = ''

    unset SOURCE_DATE_EPOCH     
    export LD_LIBRARY_PATH=${lib.makeLibraryPath [ stdenv.cc.cc ]}:$LD_LIBRARY_PATH '';
}
