.PHONY: test check smoke

# Run every suite that needs no emulator.
test:
	./run-tests.sh

# Alias of test.
check: test

# End-to-end check on an installed host with KVM.
smoke:
	./tests/smoke.sh
