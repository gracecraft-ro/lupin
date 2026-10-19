# Install

This page tells you how to set up a Lupin fleet. Each setting named here is
in `docs/configuration.md`.

Terms:

- **Fleet**: a group of machines that share one Redis server.
- **Coordinator**: the Redis server of the fleet.
- **Worker**: a machine that runs loops. It runs `lupin agent` and Herdr.
- **Dispatch machine**: a machine that runs `lupin serve` and sends work to
  workers. It runs no loops. A fleet can have no dispatch machine.
- **Host config**: the configuration of a machine, for example its NixOS
  modules. This repo does not supply systemd units. The host config owns
  them. The flake has an empty `nixosModules.default`.

## Required tools

The Lupin package puts `gh`, `git`, `tmux`, `herdr`, and `omp` on its own
`PATH`. On Linux, it also adds `systemd` and `chromium`. The
`without-omp` package does not add `omp`.

The repo records a minimum version only for Python. The "Lupin package"
column shows the version that `flake.lock` pins today. The "Example"
column shows the version on one maintainer machine on 2026-10-19. These
two columns are examples, not requirements.

<!-- markdownlint-disable MD013 -->
| Tool | Needed on | Check | Minimum | Lupin package | Example |
| --- | --- | --- | --- | --- | --- |
| Nix with flakes | All | `nix --version` | No minimum is recorded. | not applicable | 2.34.8 |
| Python | All | `python3 --version` | 3.11 (`pyproject.toml`) | 3.14.7 | 3.14.7 |
| Redis server | Coordinator | `redis-server --version` | No minimum is recorded. | not in the package (the pinned nixpkgs has 8.10.1) | not installed on that machine |
| `gh` | Workers, dispatch machine | `gh --version` | No minimum is recorded. | 2.100.0 | 2.102.0 |
| Herdr | Workers | `herdr --version` | No minimum is recorded. | 0.9.0 | 0.9.3 |
| `omp` | Workers with OMP loops | `omp --version` | No minimum is recorded. | 18.8.6 | 18.10.4 |
| `git` | Workers, dispatch machine | `git --version` | No minimum is recorded. | 2.55.0 | 2.55.0 |
| systemd | Workers, dispatch machine | `systemctl --version` | No minimum is recorded. | 261.2 | 261.3 |
<!-- markdownlint-enable MD013 -->

Notes:

- Redis ACL users came in Redis 6.0. `docs/redis-schema.md` gives each
  machine its own ACL user.
- `herdr --remote` asks to restart the remote Herdr server when the two
  Herdr versions are different. Use the same Herdr version on all machines
  if you use `lupin attach`.
- A `claude` loop needs the Claude Code program on the worker. An `omp`
  loop needs `omp`. Herdr starts the agent with `herdr agent start --kind
  claude` or `--kind omp`.

## Install Lupin

Do this step on each machine.

1. Add this flake as an input of the host config.
2. Add `packages.<system>.default` to `environment.systemPackages`. Use
   `packages.<system>.without-omp` if the machine runs no OMP loops.
3. Rebuild the machine.
4. Run `lupin --help` to check the install.

To build from a checkout, run this command in `<checkout>`:

```sh
nix build .
./result/bin/lupin --help
```

## Set up the coordinator

Do this step on one machine.

1. Install Redis.
2. Bind Redis to a private address, for example a tailnet address. Do not
   bind it to `0.0.0.0`. Write the addresses in `redis.conf`:

   ```text
   bind 127.0.0.1 PRIVATE_ADDRESS
   port 6379
   protected-mode yes
   aclfile /PATH/TO/users.acl
   ```

3. Turn on AOF persistence. The claim ledger then survives a restart:

   ```text
   appendonly yes
   appendfsync everysec
   ```

4. Make one ACL user for each machine. Limit each user to the keys
   `lupin:*` and to the commands in "ACL command list" in
   `docs/redis-schema.md`. Write one line for each machine in `users.acl`:

   <!-- markdownlint-disable MD013 -->
   ```text
   user default off
   user lupin-MACHINE on >PASSWORD ~lupin:* resetchannels -@all +ping +get +set +del +expire +pexpire +ttl +zadd +zrem +zscore +zcard +zrange +zremrangebyscore +xadd +xrange +xrevrange +scan +eval +evalsha +script|load +script|exists
   ```
   <!-- markdownlint-enable MD013 -->

5. Add `+multi +exec +pttl` to the user of each machine that runs
   `lupin roadmap`. `lupin roadmap` reads claims with `MULTI`, `EXEC`, and
   `PTTL` (`src/lupin/claims.py`, `claims_for`). These commands are not in
   the list in `docs/redis-schema.md`. Without them, `lupin roadmap` fails
   with `NoPermissionError`.
6. Keep `users.acl` readable only by the Redis user. You can write
   `#SHA256_HEX` in place of `>PASSWORD`. Then the file holds a hash, not
   the password.
7. Start Redis.
8. From another machine, check the login. Type the password when
   `redis-cli` asks for it:

   ```sh
   redis-cli -h COORDINATOR_HOST -p 6379 --user lupin-MACHINE --askpass PING
   ```

   The answer is `PONG`.

## Make the signing keys

Remote loop actions go through a command queue in Redis. Each command has
a signature. Each worker has its own signing key. A sender must have the
key of the target worker.

1. Make one key for each worker:

   ```sh
   openssl rand -hex 32
   ```

2. Give the key of each worker to `lupin agent` on that worker. Use
   `LUPIN_CMD_SIGNING_KEY` in the environment of that process only. Use a
   secret manager. Do not put the key in a command line. Do not put it in
   a global environment file. `lupin agent` does not read
   `LUPIN_CMD_SIGNING_KEYS_DIR`.
3. On each machine that sends commands to many workers (`lupin serve`,
   `lupin fleet-run`), put the keys in one directory. Use one file for each
   worker. The file name is the machine name of the worker. The machine
   name is the host name, unless the worker runs `lupin agent --machine
   NAME`.
4. Make the directory and its files readable only by the user that runs
   Lupin.
5. Set `LUPIN_CMD_SIGNING_KEYS_DIR` to the directory. As an alternative,
   load each file as a systemd credential with the worker name as the
   credential name. Lupin then reads it from `$CREDENTIALS_DIRECTORY`.
6. For one remote command from the command line (for example `lupin stop
   REPO --machine NAME`), give the key of that worker in
   `LUPIN_CMD_SIGNING_KEY`.

## Set up a worker

Do these steps on each worker. A worker must run Linux with systemd.

1. Install Lupin (see [Install Lupin](#install-lupin)). Install the agent
   programs for the platforms you use.
2. Clone each repo into the code directory. The default code directory is
   `/code`. Set `LUPIN_LOOP_CODE_DIR` to use another directory:

   ```sh
   git clone https://github.com/OWNER/REPO.git /code/REPO
   ```

3. Make the loop state directory. The default is
   `/var/lib/delegation-loop`. Set `LUPIN_LOOP_STATE_DIR` to use another
   directory. The loop user must be able to write it.
4. Let the user that runs `lupin run` and `lupin agent` run `sudo -n
   systemd-run` and `sudo -n systemctl` with no password. Lupin starts loop
   units and timers with these commands.
5. Set the loop user with `LUPIN_LOOP_USER`, or make sure that `USER` is
   set. Set `LUPIN_LOOP_GROUP` if the group is not `users`.
6. Give each repo a GitHub token. Write the path of each token file in a
   JSON file. Set `LUPIN_LOOP_CREDENTIALS_FILE` to that JSON file. The
   format is in `docs/configuration.md`.
7. Put the Redis password in a file. Do not let other users read it. Set
   `LUPIN_LOOP_REDIS_PASSWORD_FILE` to that file. systemd reads the file
   and gives the password to loop units as the credential `redis-password`.
8. Set `LUPIN_REDIS_HOST`, `LUPIN_REDIS_PORT`, and `LUPIN_REDIS_USERNAME`
   for the Lupin services. `lupin agent` and the loop worker do not read
   `fleet.json` for the host.
9. Add the machine to the fleet. Give the password through
   `LUPIN_REDIS_PASSWORD`, not in the command line:

   ```sh
   lupin join COORDINATOR_HOST:6379 --redis-username lupin-MACHINE
   ```

   This writes `~/.config/lupin/fleet.json` and the machine record in
   Redis. It does not save the password.
10. Run `lupin heartbeat` at a fixed interval. A machine record with no
    heartbeat for 120 seconds shows as offline. `docs/redis-schema.md`
    gives an interval of 30 seconds. The host config must supply the timer.
11. Run `lupin agent` as a long-running service. It polls the command queue
    and writes the periodic debriefs. The host config must supply the unit.
12. Optional: run `lupin quota` at a fixed interval on each machine that
    has provider logins. The host config must supply the timer.
13. Optional: set `LUPIN_GH_CANONICAL_FETCHER` to the same host name on
    every machine. Then one machine runs `gh` for the shared cache. Log in
    to `gh` on that machine (`gh auth login`).
14. Check the worker:

    ```sh
    lupin machines
    lupin loops
    ```

    The worker shows as `online`.

## Set up the dispatch machine

A dispatch machine is optional. Do these steps on it.

1. Install Lupin.
2. Clone each repo into the code directory, as on a worker. The Repos and
   Roadmap pages list only the repos that have a checkout there.
3. Join the fleet with `lupin join`, as on a worker. Do not run
   `lupin agent` on this machine.
4. Set `LUPIN_LOOP_COORDINATOR_ONLY=1` for `lupin serve`. The dashboard then
   does not offer this machine for "Run now".
5. Give `lupin serve` the signing keys of all workers (see
   [Make the signing keys](#make-the-signing-keys)).
6. Give `lupin serve` the Redis password as the systemd credential
   `redis-password`.
7. Run `lupin serve` as a service. Bind it to loopback or to a tailnet
   address. Lupin refuses other addresses:

   ```sh
   lupin serve --bind 127.0.0.1 --port 8788
   ```

8. Enable the repos that `fleet-run` sends to workers. `fleet-run` reads
   the enabled list on this machine:

   ```sh
   lupin enable REPO
   ```

9. Optional: run `lupin fleet-run` from a systemd timer named
   `delegation-loop.timer`. The dashboard and `lupin schedule`, `pause`,
   and `resume` control the timer with that name. The host config must
   supply the timer and its service. `lupin schedule first` and `lupin
   schedule cal` write a drop-in file at
   `<state dir>/timer-dropin.d/override.conf`. The host config must make
   systemd read that file for the timer.
10. To open the dashboard from another computer, use an SSH tunnel:

    ```sh
    ssh -N -L 8788:127.0.0.1:8788 MACHINE
    ```

    Then open `http://localhost:8788`.

`fleet-run` sends a repo only to a worker with these properties:

- The worker is `online`.
- The worker is not the local machine.
- Its record lists the action `loop.run`.
- This machine has its signing key.
- Its record lists a checkout of the repo.
- Its record lists no loop for the repo.

## Set up `lupin attach`

Do this step on each machine where you use `lupin attach`.

1. Write `~/.config/lupin/ssh-targets`. Write one line for each worker:

   ```text
   WORKER_NAME user@WORKER_HOST
   ```

2. Do not add the dispatch machine. It has no Herdr.
3. Make sure that SSH key login to each worker works.
