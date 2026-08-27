# atfc

TerraFirmaGreg Modern 0.13.8 for Minecraft 1.20.1, Forge 47.4.13, and Java 17.

`pack/` is the Packwiz source of truth. GitHub Pages publishes it at:

```text
https://<user>.github.io/<repo>/pack/pack.toml
```

The pack uses normal Distant Horizons distant generation. There is no Chunky or pregenerated-world workflow.

## Configure

Edit `pack.env` once:

```text
PACK_URL=https://<user>.github.io/<repo>/pack/pack.toml
```

Install Packwiz with the normal package/tool manager when available. The Makefile also accepts an explicit path:

```bash
make PACKWIZ=/path/to/packwiz refresh
```

## Client

Build the one-time Prism bootstrap:

```bash
make client
```

Import `dist/atfc-prism.zip` into Prism once and press Play. Each launch synchronizes the current hosted pack, so normal pack changes do not need a new zip.

## Change the pack

Re-imports of upstream releases are automated:

```bash
uv run scripts/import_tfg_release.py \
    --zip <official-curseforge.zip> --lock <tag>/pakku-lock.json \
    --prev-zip <previous-release.zip> --release-version <v> \
    --expected-sha256 <from-github-release-api>   # dry run; add --apply to write
```

It plans before it writes, keeps unchanged entries byte-stable, and preserves repo-owned doctrine from `scripts/import-overrides.json`. See `docs/tfg-modern-source-lock.md` for the stability contract and provider-skew rules.

Manual changes stay the same as before:

```bash
cd pack
packwiz <command>
cd ..
make refresh
git add -A pack scripts docs README.md .gitignore client server Makefile .nojekyll
git commit -m 'Update pack'
git push
```

## Server install

The Git checkout owns the management scripts. Its ignored mutable runtime is `server/runtime/`:

```bash
cd ~/minecraft
git clone <repo-url> atfc
cd atfc
JAVA_BIN=/usr/lib/jvm/java-17-openjdk-amd64/bin/java make install
```

The runtime is `~/minecraft/atfc/server/runtime/`. The installer creates it, records the selected Java runtime in `java.env`, installs Forge if needed, synchronizes the server Packwiz subset, renders the tracked example files only when their runtime copies are missing or empty, symlinks the user unit to the checkout, and enables the user service. It does not accept the EULA or start Minecraft.

Create `~/minecraft/atfc/server/runtime/eula.txt` with `eula=true` after reading Mojang's EULA, then start the service:

```bash
systemctl --user start minecraft-atfc.service
```

Optional host setup:

```bash
sudo loginctl enable-linger "$USER"
```

## Deploy

Configure an SSH alias named `atfc` in `~/.ssh/config`, then:

```bash
git push
make deploy
```

This runs `git pull --ff-only` in `~/minecraft/atfc/` and then `./server/update.sh`. The update stops the service, synchronizes Packwiz with the server-side selection, clears stale systemd start-limit state, and starts the service only after a successful sync. On the server itself, use `git pull --ff-only && make update`.

## Local administration

RCON is operator-managed. If it is enabled in the runtime `server.properties`, create the ignored password file:

```bash
printf '%s\n' 'RCON_PASS=replace-this' > server/runtime/.rcon_pass
chmod 600 server/runtime/.rcon_pass
```

The default RCON client is `mcrcon`. Override it when the client lives elsewhere:

```bash
make RCON_CLIENT="$HOME/path/to/mcrcon" list
```

Available commands:

```bash
make rcon
make list
make say MSG='Server restart in five minutes'
make cmd CMD='weather clear'
make save
make stop
make update
make status
make logs
make log
make restart
```

There is no server archive, SCP deployment, or generated server artifact.
