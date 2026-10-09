#!/bin/sh
# Bootstrap data + model artifacts when missing, then exec the real command.
# Idempotent: `docker compose restart` / redeploys do not retrain (named
# volumes / Fly volumes persist), and a *fingerprint guard* retrains only when
# the ledger is newer than the model bundle (stale-score prevention).
set -e

if [ ! -f data/transactions.csv ]; then
  echo 'Bootstrapping: generating data...'
  python generate_data.py --config config.yaml || { echo 'data bootstrap failed'; exit 1; }
fi
if [ ! -f model_bench/risk_model_bundle.joblib ]; then
  echo 'Bootstrapping: training + benchmarking models (first run, ~1-2 min)...'
  python model_bench/train_and_compare.py --data data/transactions.csv --config config.yaml \
    || { echo 'model bootstrap failed'; exit 1; }
elif [ data/transactions.csv -nt model_bench/risk_model_bundle.joblib ]; then
  # Fingerprint guard (E.1 cold-start requirement): artifacts exist, but the
  # ledger was regenerated after the model was trained — scores would be stale.
  # Retrain so a persistent volume never serves a model built on old data.
  echo 'Ledger newer than model bundle — retraining (fingerprint guard)...'
  python model_bench/train_and_compare.py --data data/transactions.csv --config config.yaml \
    || { echo 'model retrain failed'; exit 1; }
fi
echo 'Artifacts present — starting service.'
exec "$@"
