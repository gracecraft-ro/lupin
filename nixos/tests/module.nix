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
      credentialsFile = "/etc/lupin/credentials.json";
    };

    # The agent needs the command-queue key of this worker. The file name is
    # the worker name. A sibling key exists for another worker; the agent
    # unit must not be able to see it.
    environment.etc."lupin/keys/machine".text = "test-signing-key";
    environment.etc."lupin/keys/other".text = "other-signing-key";

    # The loop wrapper allows `LoadCredential=gh-token:` only for a path in
    # this repository-to-path map.
    environment.etc."lupin/github-token".text = "test-github-token";
    environment.etc."lupin/credentials.json".text = builtins.toJSON {
      "gracecraft-software/lupin" = "/etc/lupin/github-token";
    };

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
    # password, even with an otherwise valid unit. Run through `sudo -n` so
    # only the wrapper's validation can reject the call.
    machine.fail(
        f"sudo -u lupin sudo -n {wrapper} --unit=lupin-negative --collect"
        " --uid=lupin --gid=lupin"
        " --property=LoadCredential=leak:/etc/shadow"
        f" -- ${package}/bin/lupin"
    )

    # Loop unit names must stay in the `lupin-` namespace.
    machine.fail(
        f"sudo -u lupin sudo -n {wrapper} --unit=evil --collect"
        " --uid=lupin --gid=lupin"
        f" -- ${package}/bin/lupin"
    )

    # A call with the exact flags `loop_runtime._systemd_run` emits for a
    # loop unit must pass the allowlist. The unit stays up for the checks
    # below.
    exact_flags = (
        " --unit=lupin-probe --collect --uid=lupin --gid=lupin"
        " --property=KillMode=control-group"
        " --property=TimeoutStopSec=20s"
        " --property=Restart=on-failure"
        " --property=RestartSec=10s"
        " --property=StartLimitIntervalSec=5min"
        " --property=StartLimitBurst=3"
        " --setenv=HOME=/var/lib/delegation-loop"
        " --setenv=LUPIN_BACKEND=redis"
        " --setenv=LUPIN_REDIS_HOST=127.0.0.1"
        " --setenv=LUPIN_REDIS_PORT=6379"
        " --setenv=LUPIN_LOOP_STATE_DIR=/var/lib/delegation-loop"
        " --setenv=LUPIN_LOOP_CODE_DIR=/var/lib/lupin/code"
        " --setenv=LUPIN_LOOP_USER=lupin"
        " --setenv=LUPIN_LOOP_GROUP=lupin"
        " --setenv=LUPIN_LOOP_CREDENTIALS_FILE=/etc/lupin/credentials.json"
        f" --setenv=LUPIN_SYSTEMD_RUN={wrapper}"
        " --working-directory /var/lib/lupin/code"
    )
    machine.succeed(
        f"sudo -u lupin sudo -n {wrapper}{exact_flags}"
        f" -- ${package}/bin/lupin serve --bind 127.0.0.1 --port 8789"
    )
    machine.wait_for_unit("lupin-probe.service")

    # The wrapper adds the property itself, after validation.
    inaccessible = machine.succeed(
        "systemctl show lupin-probe.service -p InaccessiblePaths --value"
    ).strip()
    assert "/etc/lupin/keys" in inaccessible.split(), inaccessible

    # A loop unit cannot read the directory, so it cannot read the sibling
    # key or even its own key file.
    probe_pid = machine.succeed(
        "systemctl show lupin-probe.service -p MainPID --value"
    ).strip()
    machine.fail(f"cat /proc/{probe_pid}/root/etc/lupin/keys/other")
    machine.fail(f"cat /proc/{probe_pid}/root/etc/lupin/keys/machine")
    machine.succeed("systemctl stop lupin-probe.service")

    # A `gh-token` credential from the repository map is accepted.
    machine.succeed(
        f"sudo -u lupin sudo -n {wrapper} --unit=lupin-token --collect"
        " --uid=lupin --gid=lupin"
        " --property=LoadCredential=gh-token:/etc/lupin/github-token"
        " --working-directory /var/lib/lupin/code"
        f" -- ${package}/bin/lupin --help"
    )

    # A `gh-token` source outside the map is refused.
    machine.fail(
        f"sudo -u lupin sudo -n {wrapper} --unit=lupin-token-negative"
        " --collect --uid=lupin --gid=lupin"
        " --property=LoadCredential=gh-token:/etc/shadow"
        " --working-directory /var/lib/lupin/code"
        f" -- ${package}/bin/lupin --help"
    )
  '';
}
