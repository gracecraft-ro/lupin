{ pkgs ? import <nixpkgs> { }, self }:

pkgs.testers.nixosTest {
  name = "lupin-module";

  nodes.machine = { ... }: {
    imports = [ (import ../module.nix { inherit self; }) ];

    networking.hostName = "machine";

    services.lupin = {
      enable = true;
      role = "both";
      redis.username = null;
      serve.bind = "127.0.0.1";
      signingKeysDir = "/etc/lupin/keys";
    };

    # The agent needs the command-queue key of this worker. The file name is
    # the worker name.
    environment.etc."lupin/keys/machine".text = "test-signing-key";

    services.redis.servers.lupin = {
      enable = true;
      bind = "127.0.0.1";
      port = 6379;
    };
  };

  testScript = ''
    machine.start()
    machine.wait_for_unit("redis-lupin.service")
    machine.wait_for_open_port(6379)

    machine.wait_for_unit("lupin-agent.service")
    machine.wait_for_unit("lupin-serve.service")
    machine.wait_for_open_port(8788)

    # Add this machine to the fleet, then read the fleet list back.
    machine.succeed("lupin join 127.0.0.1:6379")
    machine.wait_until_succeeds(
        "lupin machines --json | grep -q '\"name\": \"machine\"'"
    )

    machine.succeed("${pkgs.curl}/bin/curl -fsS http://127.0.0.1:8788/")
    machine.succeed("systemctl is-active lupin-agent.service")
    machine.succeed("systemctl is-active lupin-serve.service")
  '';
}
