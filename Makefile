.PHONY: finetune-set pwa test seed verify g0 run lint api federate scale credit-backtest

test:
	python -m pytest -q

seed:
	python data/seed.py

verify:
	python -m munim.ledger.chain data/munim.db

g0:
	python scripts/g0_check.py

run:
	python -m munim.app

api:
	uvicorn munim.api.server:app --host 0.0.0.0 --port 8080

federate:
	python scripts/federated_round.py

scale:
	python bench/scale_bench.py 200 40

credit-backtest:
	python bench/backtest_credit.py

finetune-set:
	python scripts/make_finetune_set.py

pwa:
	@echo "PWA is served at /app by `make api` (zero-build)"
