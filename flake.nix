{
  description = "Development environment for zppy";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    git-hooks.url = "github:cachix/git-hooks.nix";
    git-hooks.inputs.nixpkgs.follows = "nixpkgs";
    zpg.url = "github:asciimoth/zpg";
    zpg.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      git-hooks,
      zpg,
      ...
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          package = pkgs.python311Packages.buildPythonPackage {
            pname = "zppy";
            version = "0.1.0";
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./pyproject.toml
                ./README.md
                ./LICENSE
                ./src
                ./tests
                ./examples
              ];
            };
            build-system = [ pkgs.python311Packages.setuptools ];
            nativeCheckInputs = [ zpg.packages.${system}.zpg ];
            pythonImportsCheck = [ "zppy" ];
            checkPhase = ''
              runHook preCheck
              PYTHONPATH=src python -m unittest discover -s tests -v
              runHook postCheck
            '';
          };
        in
        {
          default = package;
          zppy = package;
        }
      );

      checks = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          package = self.packages.${system}.default;
          pre-commit-check = git-hooks.lib.${system}.run {
            src = ./.;
            hooks = {
              check-added-large-files.enable = true;
              check-case-conflicts.enable = true;
              check-merge-conflicts.enable = true;
              check-toml.enable = true;
              check-yaml.enable = true;
              commitizen.enable = true;
              deadnix.enable = true;
              end-of-file-fixer.enable = true;
              markdownlint.enable = true;
              nixfmt.enable = true;
              ruff.enable = true;
              ruff-format.enable = true;
              statix.enable = true;
              trim-trailing-whitespace.enable = true;
              typos.enable = true;
              just-fmt = {
                enable = true;
                name = "Justfile format";
                entry = "${pkgs.just}/bin/just --fmt --check";
                files = "^justfile$";
                pass_filenames = false;
              };
            };
          };
        }
      );

      formatter = eachSystem (system: nixpkgs.legacyPackages.${system}.nixfmt);

      devShells = eachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          hook = self.checks.${system}.pre-commit-check;
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python311.withPackages (ps: [
                ps.build
                ps.setuptools
              ]))
              pkgs.just
              zpg.packages.${system}.zpg
            ]
            ++ hook.enabledPackages;
            inherit (hook) shellHook;
          };
        }
      );
    };
}
