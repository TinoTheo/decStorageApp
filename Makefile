# Shortcuts for macOS and Linux. Everything lives in tasks.py, which also works
# on Windows without make: `python tasks.py <command>`.
.PHONY: install dev test test-py test-js e2e check

install dev test test-py test-js e2e check:
	python3 tasks.py $@
