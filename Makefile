.PHONY: test lint secrets live-check trial deploy rollback
test:        ## unit tests that run anywhere
	cd needle-voice/tests && python3 -m pytest -q
	python3 -m pytest -q tests
lint:
	python3 -m compileall -q duplex live needle-voice bench.py
secrets:
	bash scripts/check_no_secrets.sh
live-check:  ## on AI2 only: end-to-end checks against a RUNNING voice server (never while someone is using it)
	live/selftest.py 2>/dev/null || /home/salil/venvs/duplex-live/bin/python live/selftest.py
trial:       ## deploy the current origin/main to a spare port and smoke-test it (does not touch the live service)
	scripts/deploy_voice.sh origin/main --trial
deploy:      ## deploy origin/main to the live port (refuses if someone is connected; rolls back on failure)
	scripts/deploy_voice.sh
rollback:    ## back to the previously deployed release
	scripts/rollback_voice.sh
