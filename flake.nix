{
  description = "lupin -- model routing and slot leases for multi-machine delegation loops";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "aarch64-linux" "x86_64-linux" "aarch64-darwin" ];
      forEachSystem = f: nixpkgs.lib.genAttrs systems f;
    in
    {
      packages = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.python3Packages.buildPythonApplication {
            pname = "lupin";
            version = "0.1.0";
            pyproject = true;
            src = self;
            build-system = [ pkgs.python3Packages.setuptools ];
            # Standard library only -- no runtime dependencies yet. A later
            # sub-issue adds python3Packages.redis for the `redis` backend.
            dependencies = [ ];
            nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
            meta = {
              description = "Model routing and slot leases for multi-machine delegation loops";
              mainProgram = "lupin";
            };
          };
        }
      );

      checks = forEachSystem (system: {
        default = self.packages.${system}.default;
      });

      # Filled in by a later sub-issue, once lupin is a real dependency of a
      # fleet config (ghostbook.nix, lab.nix). No options yet -- there is no
      # consumer to write them against.
      nixosModules.default = { config, lib, pkgs, ... }: { };
    };
}
