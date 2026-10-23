.PHONY: test test-unit test-py test-fork analyze anvil-demo build

# Phone / Codespaces friendly. `make test` never touches a live chain.

build:
	forge build

test-unit:
	forge test --no-match-path 'test/fork/*'

test-py:
	PYTHONPATH=bot python3 -m pytest -q

test: test-unit test-py

test-fork:
	bash scripts/ci-fork-tests.sh

analyze:
	PYTHONPATH=bot python3 -m flasharb.analyze

anvil-demo:
	bash scripts/anvil-demo.sh
