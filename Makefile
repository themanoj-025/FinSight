PYTHON ?= python
DATA ?= data/transactions.csv

.PHONY: help setup data data-tiny data-demo data-bench train train-bench bench all run app api report digest hooks test test-slow lint format typecheck docs-check openapi hpo hpo-promote mutate contract-fuzz loadtest a11y generate-secrets verify-secrets clean

help:
	@echo "FinSight Agent — targets:"
	@echo "  make setup      install dependencies (editable + dev extras)"
	@echo "  make data       generate the demo-tier ledger (data/transactions.csv)"
	@echo "  make data-tiny  generate a tiny ledger (fast tests / CI)"
	@echo "  make data-demo  generate the demo-tier ledger (same as make data)"
	@echo "  make data-bench generate the bench-tier ledger as Parquet (data/transactions.parquet)"
	@echo "  make train      train + benchmark models on data/transactions.csv, pick the best by mean CV PR-AUC"
	@echo "  make train-bench train + benchmark models on the bench-tier Parquet ledger"
	@echo "  make hpo         run the Optuna HPO study over the LightGBM family (opt-in; writes the importance chart + study db)"
	@echo "  make hpo-promote re-run HPO and adopt the tuned params for the next make train (requires the min_improvement gate)"
	@echo "  make mutate     run mutation testing against rules.py + features.py (D.4; slow, opt-in)"
	@echo "  make bench      data-bench + train-bench (the full nightly-quality pipeline)"
	@echo "  make all        data + train"
	@echo "  make run        bootstrap everything and launch the Streamlit app"
	@echo "  make api        run the FastAPI facts service (docs at http://localhost:8000/docs)"
	@echo "  make report     write the monthly report as Markdown + branded PDF"
	@echo "  make digest     build + deliver the weekly digest (Slack/email if configured)"
	@echo "  make hooks      enable git hooks (pre-push lockfile-drift check)"
	@echo "  make test       fast pytest suite (excludes slow-marked tests)"
	@echo "  make test-slow  slow suite: server boot + 100k-row perf"
	@echo "  make lint       ruff check + format check"
	@echo "  make typecheck  mypy on the whole project (finance_agent/ model_bench/ app/ tests/)"
	@echo "  make docs-check docs-vs-code consistency (schema, tests, security claims)"
	@echo "  make openapi    freeze the FastAPI OpenAPI schema to docs/technical/openapi.v1.json (E.2)"
	@echo "  make contract-fuzz  schemathesis fuzz the API against the committed OpenAPI contract (F.3; slow, opt-in)"
	@echo "  make loadtest   Locust load test vs the SLOs (F.5; requires the API up — make api)"
	@echo "  make a11y       axe-core accessibility + mobile render check over all pages (F.4; needs .[a11y])"
	@echo "  make generate-secrets  mint strong APP_PASSWORD / FINSIGHT_API_KEY / FINSIGHT_BUNDLE_KEY values (audit §1a)"
	@echo "  make verify-secrets    prove the deployment gates are enforced (needs FINSIGHT_URL + FINSIGHT_API_KEY)"
	@echo "  make clean      remove generated artifacts (keeps tracked metadata)"

setup:
	$(PYTHON) -m pip install -e ".[dev]"

data: generate_data.py
	$(PYTHON) generate_data.py --tier demo --config config.yaml

data-tiny: generate_data.py
	$(PYTHON) generate_data.py --tier tiny --config config.yaml

data-demo: data

data-bench: generate_data.py
	$(PYTHON) generate_data.py --tier bench --config config.yaml

train: $(DATA)
	$(PYTHON) model_bench/train_and_compare.py --data $(DATA) --config config.yaml

train-bench: data/transactions.parquet
	$(PYTHON) model_bench/train_and_compare.py --data data/transactions.parquet --config config.yaml

hpo: $(DATA)
	$(PYTHON) model_bench/train_and_compare.py --hpo --config config.yaml

hpo-promote: $(DATA)
	$(PYTHON) model_bench/train_and_compare.py --hpo --promote --config config.yaml

mutate:
	# Scope + test selection are config-driven ([tool.mutmut] in pyproject.toml)
	$(PYTHON) -m mutmut run
	$(PYTHON) -m mutmut results --all true

contract-fuzz:
	# F.3 — boots a throwaway API, fuzzes every operation in the committed
	# OpenAPI schema, fails on 500s / schema drift. Needs .[contract].
	$(PYTHON) scripts/contract_fuzz.py --port 8123

loadtest:
	# F.5 — 50 users / 120s against the cached-facts endpoints, then compares
	# p95 + error rate against docs/technical/SLOs.md (pass/fail). Needs
	# .[loadtest] AND a running API (make api) — boot first, then run this.
	$(PYTHON) -m locust -f loadtest/locustfile.py --host http://127.0.0.1:8000 \
		--headless -u 50 -r 2 --run-time 120s --csv loadtest/results \
		--html loadtest/report.html --logfile /dev/null
	$(PYTHON) scripts/loadtest_check.py --csv loadtest/results_stats.csv

a11y:
	# F.4 — Playwright + axe-core over Home + all 7 pages, plus a 375px
	# mobile-overflow check on the two layout-dense pages. Needs .[a11y] and
	# `playwright install chromium`. Boots its own Streamlit instance.
	$(PYTHON) scripts/accessibility_check.py

generate-secrets:
	# Audit §1a — mint strong, unique values for APP_PASSWORD / FINSIGHT_API_KEY /
	# FINSIGHT_BUNDLE_KEY and print them (never persisted; store in a password
	# manager). Then set them on the deploy target and run `make verify-secrets`.
	./scripts/generate_secrets.sh

verify-secrets:
	# Audit §1a — self-check that a deployment's gates are actually enforced:
	# /api/* returns 401 without X-API-Key and 200 with the correct one, and the
	# app is reachable (password prompt = manual browser check). Needs
	# FINSIGHT_URL (default http://localhost:8000) + FINSIGHT_API_KEY set.
	$(PYTHON) scripts/verify_secrets.py

bench: data-bench train-bench

all: data train

run: all
	streamlit run app/Home.py

app:
	streamlit run app/Home.py

api: $(DATA)
	uvicorn finance_agent.api:app --reload --port 8000

report: $(DATA)
	$(PYTHON) -m finance_agent report --pdf

digest: $(DATA)
	$(PYTHON) -m finance_agent digest

hooks:
	git config core.hooksPath .githooks
	@echo "Git hooks enabled — .githooks/pre-push will verify requirements.lock stays in sync with pyproject.toml before every push."
	@echo "(Run the check manually any time with: scripts/check_lockfile.sh)"
	@echo "(If git ever reports the pre-push hook was 'ignored because it's not set as executable', run: chmod +x .githooks/pre-push)"

test:
	$(PYTHON) -m pytest tests/ -m "not slow"

test-slow:
	$(PYTHON) -m pytest tests/ -m slow

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy finance_agent model_bench app tests

docs-check:
	$(PYTHON) scripts/check_docs_consistency.py

openapi:
	$(PYTHON) scripts/export_openapi.py --out docs/technical/openapi.v1.json

format:
	ruff format .
	ruff check --fix .

clean:
	rm -f data/transactions.csv data/agent_activity.jsonl
	rm -f model_bench/best_model.joblib model_bench/risk_model_bundle.joblib
	rm -f model_bench/*.joblib.sig   # C.2.4 bundle signatures (regenerated by `make train`)
	rm -rf model_bench/results reports
	# NOTE: model_bench/best_model_metadata.json is intentionally kept — it is
	# tracked in git and regenerated by `make train`; deleting it would leave a
	# dirty working tree after every clean.
