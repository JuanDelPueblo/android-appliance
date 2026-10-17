.PHONY: test check e2e e2e-webrtc

# Run every suite that needs no emulator.
test:
	./run-tests.sh

# Alias of test.
check: test

# End-to-end test of v2 on a host with KVM and Podman: installs its own
# instance from the built image, tests it, and removes it again.
e2e:
	./tests/e2e/lifecycle.sh

# End-to-end test of the v2 dashboard in Chromium against a real Android
# (needs Playwright, see README): installs its own instance, tests it, and
# removes it again.
e2e-webrtc:
	./tests/e2e/webrtc.sh
