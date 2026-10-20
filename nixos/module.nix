# The Lupin fleet module: one systemd service user, the agent, the dashboard,
# the quota timer, and the `fleet-run` timer.
{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.lupin;

  # The flake's own package, unless the host config overrides it.
  package =
    if cfg.package != null
    then cfg.package
    else self.packages.${pkgs.stdenv.hostPlatform.system}.default;

  isWorker = cfg.role == "worker" || cfg.role == "both";
  isDispatch = cfg.role == "dispatch" || cfg.role == "both";

  fleetConfigPath =
    if cfg.fleetConfig != null then cfg.fleetConfig else "/etc/lupin/fleet.json";

  # `StateDirectory=` takes a name under /var/lib, not an absolute path.
  stateDirectoryName =
    let
      match = builtins.match "/var/lib/([^/]+)" cfg.stateDir;
    in
    if match == null then null else builtins.head match;

  stateDirectory = lib.optionalAttrs (stateDirectoryName != null) {
    StateDirectory = stateDirectoryName;
    StateDirectoryMode = "0750";
  };

  # Every Lupin unit gets the same fleet location and loop directories, so a
  # loop started from `lupin run` and a command handled by `lupin agent` use
  # the same state.
  commonEnvironment = {
    HOME = cfg.stateDir;
    LUPIN_LOOP_STATE_DIR = cfg.stateDir;
    LUPIN_LOOP_CODE_DIR = cfg.codeDir;
    LUPIN_LOOP_USER = cfg.user;
    LUPIN_LOOP_GROUP = cfg.group;
    LUPIN_FLEET_CONFIG = fleetConfigPath;
    LUPIN_BACKEND = "redis";
    LUPIN_REDIS_HOST = cfg.redis.host;
    LUPIN_REDIS_PORT = toString cfg.redis.port;
  }
  // lib.optionalAttrs (cfg.redis.username != null) {
    LUPIN_REDIS_USERNAME = cfg.redis.username;
  }
  // lib.optionalAttrs (cfg.redis.passwordFile != null) {
    LUPIN_LOOP_REDIS_PASSWORD_FILE = toString cfg.redis.passwordFile;
  };

  # The units that sign commands for other workers need the directory.
  # `lupin agent` reads only its own key, as a credential, so a loop unit
  # that runs as the same user cannot see the other workers' keys.
  signingKeysEnvironment = lib.optionalAttrs (cfg.signingKeysDir != null) {
    LUPIN_CMD_SIGNING_KEYS_DIR = toString cfg.signingKeysDir;
  };

  # The Redis password is a systemd credential. No unit gets it in its
  # environment, so it cannot show up in `systemctl show` or `/proc`.
  credentials = lib.optionalAttrs (cfg.redis.passwordFile != null) {
    LoadCredential = [ "redis-password:${toString cfg.redis.passwordFile}" ];
  };

  # The command queue signing keys are read from this directory. Serve and
  # `fleet-run` sign commands for every worker; the agent only needs the key
  # of this worker. Mount the keys read-only for the units that need the
  # whole directory; the service user never writes one.
  signingKeyMount = lib.optionalAttrs (cfg.signingKeysDir != null) {
    BindReadOnlyPaths = [ (toString cfg.signingKeysDir) ];
  };

  # A credential is one file, so the agent's unit sees only its own key.
  # `commands.signing_key_for` reads `$CREDENTIALS_DIRECTORY/<hostName>` when
  # `LUPIN_CMD_SIGNING_KEYS_DIR` is unset.
  agentSigningKeyCredential = lib.optionalAttrs (cfg.signingKeysDir != null) {
    LoadCredential = (credentials.LoadCredential or [ ]) ++ [
      "${config.networking.hostName}:${toString cfg.signingKeysDir}/${config.networking.hostName}"
    ];
  };

  # `systemd-run` can become root with `--uid=0` or `--property=User=root`,
  # so the NOPASSWD rule names this validator, not `systemd-run`. It accepts
  # only the options Lupin passes and only the Lupin command.
  systemdRunWrapper = pkgs.writeShellScript "lupin-systemd-run" ''
    set -eu

    systemd_run="${pkgs.systemd}/bin/systemd-run"
    lupin="${package}/bin/lupin"
    allowed_user="${cfg.user}"
    allowed_group="${cfg.group}"
    code_dir="$(${pkgs.coreutils}/bin/realpath -m -- "${cfg.codeDir}")"
    # The only credential a loop unit may load. Empty when the module has no
    # Redis password file, which rejects every `LoadCredential`.
    expected_credential_path=${lib.escapeShellArg (if cfg.redis.passwordFile != null then toString cfg.redis.passwordFile else "")}

    fail() {
      printf 'lupin-systemd-run: %s\n' "$*" >&2
      exit 1
    }

    check_unit() {
      [ -n "$1" ] || fail "empty unit name"
      case "$1" in
        lupin-*) ;;
        *) fail "unit name must start with lupin-: $1" ;;
      esac
      case "$1" in
        *[!A-Za-z0-9_.@:-]*) fail "invalid unit name: $1" ;;
      esac
    }

    check_credential_name() {
      [ -n "$1" ] || fail "empty credential name"
      case "$1" in
        *[!A-Za-z0-9_.-]*) fail "invalid credential name: $1" ;;
      esac
    }

    check_working_directory() {
      [ -n "$1" ] || fail "empty working directory"
      local resolved
      resolved="$(${pkgs.coreutils}/bin/realpath -m -- "$1")"
      case "$resolved" in
        "$code_dir" | "$code_dir"/*) ;;
        *) fail "working directory is outside $code_dir: $1" ;;
      esac
    }

    original=("$@")
    seen_command=0
    after_separator=0

    while [ "$#" -gt 0 ]; do
      arg="$1"
      shift

      if [ "$after_separator" = 1 ]; then
        if [ "$seen_command" = 0 ]; then
          [ "$arg" = "$lupin" ] || fail "command must be $lupin"
          seen_command=1
        fi
        continue
      fi

      case "$arg" in
        --)
          after_separator=1
          ;;
        --collect)
          ;;
        --unit=*)
          check_unit "''${arg#--unit=}"
          ;;
        --uid=*)
          [ "''${arg#--uid=}" = "$allowed_user" ] || fail "refusing --uid"
          ;;
        --gid=*)
          [ "''${arg#--gid=}" = "$allowed_group" ] || fail "refusing --gid"
          ;;
        --property=*)
          property="''${arg#--property=}"
          [ "$property" != "''${property%%=*}" ] || fail "property without a value: $arg"
          key="''${property%%=*}"
          value="''${property#*=}"
          case "$key" in
            KillMode | TimeoutStopSec | Restart | RestartSec | StartLimitIntervalSec | StartLimitBurst | OOMPolicy | MemoryMax)
              ;;
            LoadCredential)
              name="''${value%%:*}"
              source="''${value#"$name:"}"
              [ "$name" != "$value" ] || fail "LoadCredential needs name:path"
              check_credential_name "$name"
              [ -n "$source" ] || fail "LoadCredential needs a path"
              [ "$name" = "redis-password" ] || fail "refusing credential: $name"
              [ -n "$expected_credential_path" ] || fail "no credential is configured"
              case "$source" in
                /*) ;;
                *) fail "credential source must be absolute: $source" ;;
              esac
              [ "$source" = "$expected_credential_path" ] || fail "refusing credential source: $source"
              ;;
            *)
              fail "refusing property: $key"
              ;;
          esac
          ;;
        --setenv=*)
          assignment="''${arg#--setenv=}"
          [ "$assignment" != "''${assignment%%=*}" ] || fail "setenv without a value: $arg"
          name="''${assignment%%=*}"
          case "$name" in
            PATH | HOME | ANTHROPIC_BASE_URL | HEADROOM_TELEMETRY_DISABLED | RTK_TELEMETRY_DISABLED | CODEGRAPH_TELEMETRY | DO_NOT_TRACK | LUPIN_*)
              ;;
            *)
              fail "refusing setenv: $name"
              ;;
          esac
          ;;
        --working-directory=*)
          check_working_directory "''${arg#--working-directory=}"
          ;;
        --working-directory)
          [ "$#" -gt 0 ] || fail "--working-directory needs a path"
          check_working_directory "$1"
          shift
          ;;
        --timer-property=AccuracySec=*)
          ;;
        --on-active=* | --on-calendar=*)
          ;;
        *)
          fail "refusing argument: $arg"
          ;;
      esac
    done

    [ "$seen_command" = 1 ] || fail "missing the $lupin command"
    exec "$systemd_run" "''${original[@]}"
  '';

  # The agent, the dashboard, and `fleet-run` start loop units with the
  # validated `systemd-run` wrapper and read the per-repository token map.
  controllerEnvironment = {
    LUPIN_SYSTEMD_RUN = "${systemdRunWrapper}";
  }
  // lib.optionalAttrs (cfg.credentialsFile != null) {
    LUPIN_LOOP_CREDENTIALS_FILE = toString cfg.credentialsFile;
  };

  # A dispatch-only machine does not run loops itself. The dashboard uses
  # this to hide the loop start control.
  dispatchEnvironment = lib.optionalAttrs (cfg.role == "dispatch") {
    LUPIN_LOOP_COORDINATOR_ONLY = "1";
  };

  commonServiceConfig = {
    User = cfg.user;
    Group = cfg.group;
  }
  // stateDirectory
  // credentials;
in
{
  options.services.lupin = {
    enable = lib.mkEnableOption "the Lupin fleet services";

    role = lib.mkOption {
      type = lib.types.enum [ "worker" "dispatch" "both" ];
      default = "worker";
      description = ''
        The job of this machine. A worker runs `lupin agent` and loops. A
        dispatch machine runs `lupin serve` and `fleet-run`. `both` runs
        every service.
      '';
    };

    package = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      defaultText = lib.literalExpression "self.packages.\${system}.default";
      description = ''
        The Lupin package. The default is the flake's
        `packages.<system>.default`.
      '';
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "lupin";
      description = "The user that runs the Lupin services.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "lupin";
      description = "The group of the Lupin user and of the fleet config file.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/delegation-loop";
      description = "The loop state directory.";
    };

    codeDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/lupin/code";
      description = "The directory with the repository checkouts.";
    };

    redis = {
      host = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "The host name or address of the Redis coordinator.";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 6379;
        description = "The TCP port of the Redis coordinator.";
      };

      username = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "The Redis ACL user. Null uses the Redis `default` user.";
      };

      passwordFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        description = ''
          A file that contains the Redis password. The module loads it as the
          systemd credential `redis-password`.
        '';
      };
    };

    fleetConfig = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        A fleet config file (`lupin join` format). The default writes
        `/etc/lupin/fleet.json` from the `redis` options.
      '';
    };

    signingKeysDir = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        A directory with one command-queue signing key for each worker. Each
        file name is a worker name. `lupin serve` and `fleet-run` get the
        directory read-only. The agent gets only the key of this machine, as
        the systemd credential `<hostName>`.
      '';
    };

    credentialsFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        A JSON object that maps a repository name to the path of its GitHub
        token file. The agent, the dashboard, and `fleet-run` read it as
        `LUPIN_LOOP_CREDENTIALS_FILE`.
      '';
    };

    serve = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = isDispatch;
        description = "Run the `lupin serve` dashboard.";
      };

      bind = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = ''
          The address of the dashboard. Use a loopback or tailnet address. Do
          not use `0.0.0.0`.
        '';
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 8788;
        description = "The TCP port of the dashboard.";
      };
    };

    agent = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = isWorker;
        description = "Run the `lupin agent` command-queue service.";
      };
    };

    quota = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Run `lupin quota` on a timer.";
      };

      interval = lib.mkOption {
        type = lib.types.str;
        default = "5min";
        description = "The time between two `lupin quota` runs.";
      };
    };

    fleetRun = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = isDispatch;
        description = "Run `lupin fleet-run` on a timer.";
      };

      schedule = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 00,05,10,15,20:15:00";
        description = "The `OnCalendar` setting of the `delegation-loop` timer.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.serve.bind != "0.0.0.0";
        message = ''
          services.lupin.serve.bind is 0.0.0.0. The dashboard has no
          authentication. Use 127.0.0.1, a tailnet address, or an SSH
          forward.
        '';
      }
    ];

    users.groups.${cfg.group} = { };

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
      createHome = true;
      description = "Lupin fleet services";
    };

    environment.systemPackages = [ package ];

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 ${cfg.user} ${cfg.group} -"
      "d ${cfg.codeDir} 0750 ${cfg.user} ${cfg.group} -"
      # `lupin schedule` writes its timer override here. The module links the
      # directory into systemd so a daemon-reload picks the override up.
      "d ${cfg.stateDir}/timer-dropin.d 0750 ${cfg.user} ${cfg.group} -"
      "L+ /etc/systemd/system/delegation-loop.timer.d - - - - ${cfg.stateDir}/timer-dropin.d"
    ];

    # When no existing file is given, the module writes the fleet location.
    # The file holds no password.
    environment.etc = lib.mkIf (cfg.fleetConfig == null) {
      "lupin/fleet.json" = {
        text = builtins.toJSON (
          {
            redis_host = cfg.redis.host;
            redis_port = cfg.redis.port;
          }
          // lib.optionalAttrs (cfg.redis.username != null) {
            redis_username = cfg.redis.username;
          }
        );
        mode = "0640";
        user = "root";
        group = cfg.group;
      };
    };

    systemd.services.lupin-agent = lib.mkIf cfg.agent.enable {
      description = "Lupin fleet command agent";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = commonEnvironment // controllerEnvironment;
      serviceConfig = commonServiceConfig // agentSigningKeyCredential // {
        ExecStart = "${package}/bin/lupin agent";
        Restart = "always";
        RestartSec = 5;
      };
    };

    systemd.services.lupin-quota = lib.mkIf cfg.quota.enable {
      description = "Lupin provider quota";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = commonEnvironment;
      serviceConfig = commonServiceConfig // {
        Type = "oneshot";
        ExecStart = "${package}/bin/lupin quota";
      };
    };

    systemd.timers.lupin-quota = lib.mkIf cfg.quota.enable {
      description = "Run `lupin quota` every ${cfg.quota.interval}";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1min";
        OnUnitActiveSec = cfg.quota.interval;
      };
    };

    systemd.services.lupin-serve = lib.mkIf cfg.serve.enable {
      description = "Lupin fleet dashboard";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = commonEnvironment // signingKeysEnvironment // controllerEnvironment // dispatchEnvironment;
      serviceConfig = commonServiceConfig // signingKeyMount // {
        ExecStart = "${package}/bin/lupin serve --bind ${cfg.serve.bind} --port ${toString cfg.serve.port}";
        Restart = "always";
        RestartSec = 5;
      };
    };

    # The dashboard reads `delegation-loop.timer`, and `lupin schedule`,
    # `pause`, and `resume` control that name. Do not rename it.
    systemd.services.delegation-loop = lib.mkIf cfg.fleetRun.enable {
      description = "Dispatch enabled Lupin loops to fleet workers";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = commonEnvironment // signingKeysEnvironment // controllerEnvironment // dispatchEnvironment;
      serviceConfig = commonServiceConfig // signingKeyMount // {
        Type = "oneshot";
        ExecStart = "${package}/bin/lupin fleet-run";
      };
    };

    systemd.timers.delegation-loop = lib.mkIf cfg.fleetRun.enable {
      description = "Lupin fleet-run schedule";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.fleetRun.schedule;
        Persistent = true;
      };
    };

    # This is the loop's control path: `loop_runtime.py` starts loop units and
    # controls the `delegation-loop` timer with `sudo -n`. The rules name each
    # command and the `lupin-*` and `delegation-loop.*` units only. The
    # `systemd-run` entry is the validating wrapper above; `systemd-run`
    # itself never gets NOPASSWD.
    security.sudo.extraRules = [
      {
        users = [ cfg.user ];
        commands = [
          {
            command = "${systemdRunWrapper}";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl start lupin-*.service";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl stop lupin-*.service";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl restart lupin-*.service";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl is-active lupin-*.service";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl start delegation-loop.*";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl stop delegation-loop.*";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl restart delegation-loop.*";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl is-active delegation-loop.*";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl list-timers delegation-loop.*";
            options = [ "NOPASSWD" ];
          }
          {
            command = "/run/current-system/sw/bin/systemctl daemon-reload";
            options = [ "NOPASSWD" ];
          }
        ];
      }
    ];
  };
}
