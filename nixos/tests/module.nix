{ pkgs ? import <nixpkgs> { }, self }:

let
  package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
in
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
    # the worker name. A sibling key exists for another worker; the agent
    # unit must not be able to see it.
    environment.etc."lupin/keys/machine".text = "test-signing-key";
    environment.etc."lupin/keys/other".text = "other-signing-key";

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

    # The agent loads exactly one signing key, its own. The sibling key stays
    # in the directory and out of the unit. `LoadCredential` prints as
    # unprintable, so read the unit's credential directory instead.
    pid = machine.succeed(
        "systemctl show lupin-agent.service -p MainPID --value"
    ).strip()
    credentials = machine.succeed(
        f"ls /proc/{pid}/root/run/credentials/lupin-agent.service"
    ).split()
    assert credentials == ["machine"], credentials
    key = machine.succeed(
        f"cat /proc/{pid}/root/run/credentials/lupin-agent.service/machine"
    ).strip()
    assert key == "test-signing-key", key

    # Loop units copy only the variables that `loop_runtime._service_environment`
    # forwards. The agent no longer has the keys directory in its environment,
    # so no loop unit can get it.
    agent_env = machine.succeed(
        "systemctl show lupin-agent.service -p Environment --value"
    )
    assert "LUPIN_CMD_SIGNING_KEYS_DIR" not in agent_env, agent_env

    wrapper = machine.succeed(
        "systemctl show lupin-agent.service -p Environment --value"
        " | tr ' ' '\n' | sed -n 's/^LUPIN_SYSTEMD_RUN=//p'"
    ).strip()
    assert wrapper.startswith("/nix/store/"), wrapper

    # The wrapper refuses a credential that is not the configured Redis
    # password, even with an otherwise valid unit.
    machine.fail(
        f"sudo -u lupin {wrapper} --unit=lupin-negative --collect"
        " --uid=lupin --gid=lupin"
        " --property=LoadCredential=leak:/etc/shadow"
        f" -- ${package}/bin/lupin"
    )

    # Loop unit names must stay in the `lupin-` namespace.
    machine.fail(
        f"sudo -u lupin {wrapper} --unit=evil --collect"
        " --uid=lupin --gid=lupin"
        f" -- ${package}/bin/lupin"
    )
  '';
}
