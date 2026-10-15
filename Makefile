.PHONY: test lint secrets live-check
test:        ## unit tests that run anywhere
	cd needle-voice/tests && python3 -m pytest -q
	python3 -m pytest -q tests
lint:
	python3 -m compileall -q duplex live needle-voice bench.py
secrets:
	bash scripts/check_no_secrets.sh
live-check:  ## on AI2 only: end-to-end checks against a RUNNING voice server (never while someone is using it)
	live/selftest.py 2>/dev/null || /home/salil/venvs/duplex-live/bin/python live/selftest.py
