.PHONY: test check smoke e2e

# Run every suite that needs no emulator.
test:
	./run-tests.sh

# Alias of test.
check: test

# End-to-end check of an installed v1 appliance (host with KVM).
smoke:
	./tests/smoke.sh

# End-to-end test of v2 on a host with KVM and Podman: installs its own
# instance from the built image, tests it, and removes it again.
e2e:
	./tests/e2e/lifecycle.sh
