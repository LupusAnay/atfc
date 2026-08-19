SHELL := /bin/bash

PACKWIZ ?= packwiz
JAVA_BIN ?= java
RUNTIME := $(CURDIR)/server/runtime
RCON_CLIENT ?= mcrcon
RCON_HOST ?= 127.0.0.1
RCON_PORT ?= 25575
RCON_PASS_FILE ?= $(RUNTIME)/.rcon_pass
-include $(RCON_PASS_FILE)
RCON_BASE := "$(RCON_CLIENT)" -H "$(RCON_HOST)" -P "$(RCON_PORT)" -p "$(RCON_PASS)" -c

.PHONY: help refresh client install update deploy rcon-check rcon list say cmd save stop restart status logs log

help:
	@printf '%s\n' \
	  'make refresh  Refresh pack/index.toml with Packwiz.' \
	  'make client   Build dist/atfc-prism.zip.' \
	  'make install  Install the server runtime from this checkout.' \
	  'make update   Synchronize and restart the local server.' \
	  'make deploy   Pull and update the server through SSH alias atfc.' \
	  'make rcon     Open an interactive local RCON console.' \
	  'make list     List online players.' \
	  'make say MSG=...  Broadcast a message.' \
	  'make cmd CMD=...  Run an arbitrary RCON command.' \
	  'make save     Run save-all through RCON.' \
	  'make stop     Shut down Minecraft through RCON.' \
	  'make restart  Restart the local user service.' \
	  'make status   Show the local user service status.' \
	  'make logs     Follow the local service log.' \
	  'make log      Show the recent local service log.'

refresh:
	cd pack && "$(PACKWIZ)" refresh

client:
	PACKWIZ="$(PACKWIZ)" ./scripts/build-client.sh

install:
	JAVA_BIN="$(JAVA_BIN)" ./server/install.sh

update:
	JAVA_BIN="$(JAVA_BIN)" ./server/update.sh

deploy:
	ssh atfc 'cd "$$HOME/minecraft/atfc" && git pull --ff-only && ./server/update.sh'

rcon-check:
	@command -v "$(RCON_CLIENT)" >/dev/null 2>&1 || { printf '%s\n' 'RCON client not found. Set RCON_CLIENT=/path/to/mcrcon.' >&2; exit 1; }
	@test -n "$(RCON_PASS)" || { printf '%s\n' 'Missing RCON_PASS. Create server/runtime/.rcon_pass.' >&2; exit 1; }

rcon: rcon-check
	@$(RCON_BASE) -t

list: rcon-check
	@$(RCON_BASE) 'list'

say: rcon-check
	@test -n "$(MSG)" || { printf '%s\n' 'Set MSG, for example: make say MSG="Hello".' >&2; exit 1; }
	@$(RCON_BASE) "say $(MSG)"

cmd: rcon-check
	@test -n "$(CMD)" || { printf '%s\n' 'Set CMD, for example: make cmd CMD=save-all.' >&2; exit 1; }
	@$(RCON_BASE) "$(CMD)"

save: rcon-check
	@$(RCON_BASE) 'save-all'

stop: rcon-check
	@$(RCON_BASE) 'stop'

restart:
	systemctl --user restart minecraft-atfc.service

status:
	systemctl --user status --no-pager minecraft-atfc.service

logs:
	journalctl --user -u minecraft-atfc.service -f -n 10000

log:
	journalctl --user -u minecraft-atfc.service --no-pager -e | less -R
