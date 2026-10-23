.PHONY: test test-unit test-py test-fork analyze liquidations mempool anvil-demo build

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

liquidations:
	PYTHONPATH=bot python3 -m flasharb.liquidations

mempool:
	PYTHONPATH=bot python3 -m flasharb.mempool

anvil-demo:
	bash scripts/anvil-demo.sh
