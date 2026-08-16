# Auto-TFC

Private Auto-TerraFirmaCraft 1.11.1 for Minecraft 1.18.2, Forge 40.2.9, and Java 17.

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

```bash
cd ~/minecraft/atfc/pack
packwiz <command>
packwiz refresh
cd ..
git add pack pack.env client Makefile scripts server README.md .gitignore
git commit -m 'Update pack'
git push
```

## Server install

The server checkout and mutable Minecraft runtime are separate:

```bash
cd ~/minecraft
git clone <repo-url> atfc
cd atfc
./server/install.sh
```

The runtime is `~/minecraft/servers/atfc/`. The installer creates it, installs Forge if needed, synchronizes the server Packwiz subset, creates missing `server.properties` and `user_jvm_args.txt`, and enables the user service. It does not accept the EULA or start Minecraft.

Create `~/minecraft/servers/atfc/eula.txt` with `eula=true` after reading Mojang's EULA, then start the service:

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

This runs `git pull --ff-only` in `~/minecraft/atfc/` and then `./server/update.sh`. The update stops the service, synchronizes Packwiz with the server-side selection, and starts the service only after a successful sync.

```bash
make logs
make restart
```

There is no server archive, SCP deployment, or generated server artifact.
