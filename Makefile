SHELL := /bin/bash

PACKWIZ ?= packwiz

.PHONY: help refresh client dist deploy logs restart

help:
	@printf '%s\n' \
	  'make refresh  Refresh pack/index.toml with Packwiz.' \
	  'make client   Build dist/atfc-prism.zip.' \
	  'make dist     Build the Prism bootstrap.' \
	  'make deploy   Pull and update the server through SSH alias atfc.' \
	  'make logs     Follow the server service log through SSH alias atfc.' \
	  'make restart  Restart the server service through SSH alias atfc.'

refresh:
	cd pack && "$(PACKWIZ)" refresh

client:
	PACKWIZ="$(PACKWIZ)" ./scripts/build-client.sh

dist: client

deploy:
	ssh atfc 'cd "$$HOME/minecraft/atfc" && git pull --ff-only && ./server/update.sh'

logs:
	ssh atfc 'journalctl --user -u minecraft-atfc.service -f'

restart:
	ssh atfc 'systemctl --user restart minecraft-atfc.service'
