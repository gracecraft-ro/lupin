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
            # python3Packages.redis is the `redis` backend's only runtime
            # dependency (issue #210).
            dependencies = [ pkgs.python3Packages.redis ];
            # pkgs.redis is the server binary -- tests spin up a real
            # redis-server subprocess rather than mocking the client.
            nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook pkgs.redis ];
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
