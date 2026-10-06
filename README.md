# FinSight Agent

<div align="center">

<p>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg" />
    <img src="assets/logo.svg" width="280" alt="FinSight Agent logo — candlestick tile and wordmark" />
  </picture>
</p>

# FinSight Agent

**Turns raw bank transactions into fraud alerts, spending insight, and plain-English advice — autonomously.**

![Python](https://img.shields.io/badge/Python-3.10%20%7C%203.12-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
[![CI](https://github.com/themanoj-025/FinSight/actions/workflows/ci.yml/badge.svg)](https://github.com/themanoj-025/FinSight/actions/workflows/ci.yml)
![Models](https://img.shields.io/badge/models-6%20benchmarked-2563EB)
![PR-AUC](https://img.shields.io/badge/CV%20PR--AUC-0.828%20±%200.055-16A34A)
![Stack](https://img.shields.io/badge/Stack-Streamlit%20·%20FastAPI%20·%20LightGBM-64748B)

<!--
  Social preview (maintainer note — invisible when rendered):
  GitHub does not use the README header image for the repo card. Upload one manually:
  Settings → General → Social preview → Edit → upload a 1280×640 (2:1) PNG under 1 MB.
  Good hero candidates from this repo: a capture of the Streamlit dashboard (app/), or the
  model-benchmark chart from model_bench/. Re-upload to replace; GitHub caches the previous image.
-->
![Offline](https://img.shields.io/badge/works%20offline-yes-16A34A)

*An end-to-end, agentic personal-finance system: deterministic synthetic data → feature engineering → 6-model benchmark with honest time-series CV → a hybrid **rules + ML + LLM** agent you can question in plain English — all behind a polished Streamlit app.*

</div>

---

## 📋 Table of Contents

- [💡 Why I Built This](#-why-i-built-this)
- [⚠️ Known Limitations](#️-known-limitations)
- [✨ Features](#-features)
- [🏗️ Architecture](#️-architecture)
- [🚀 Quick Start](#-quick-start)
- [📁 Project structure](#-project-structure)
- [🧪 Testing](#-testing)
- [🤝 Contributing](#-contributing)
- [📄 License](#-license)

---

## 💡 Why I Built This

I built FinSight because I was frustrated with fraud detection tutorials that just print a meaningless accuracy score on a static dataset. I wanted to build a true end-to-end ML pipeline with honest time-series cross-validation, while exploring how to safely integrate LLMs into a deterministic rules engine without letting them hallucinate financial data.

## ⚠️ Known Limitations

- **FAISS Latency:** The similar-transaction retrieval uses a local FAISS index which rebuilds in-memory. This scales poorly past 1M rows and should be replaced with pgvector in a real deployment.
- **Synthetic Data Drift:** While the synthetic ledger generator has 15 fraud patterns, it's ultimately still synthetic. It won't capture the true adversarial drift seen in real-world credit card fraud.
- **Agent Token Usage:** Using Claude/Gemini to narrate every single flagged transaction can quickly rack up API costs. The offline deterministic fallback helps, but the LLM route isn't cost-effective for batch processing.

---

## ✨ Features

| | Capability |
|---|---|
| 🧾 | **Synthetic ledger generator** — deterministic and vectorized, with **three tiers** (`tiny` for CI, `demo` for the app, `bench` for the model benchmark up to millions of rows). Multi-persona population (6 archetypes), multi-account structure (checking/savings/credit), seasonality + annual raises + drift, a geography/merchant taxonomy, and a **15-pattern difficulty-graded fraud library** with per-archetype labels. Balances always chain correctly. |
| ⚖️ | **Hybrid risk scoring** — hand-written audit rules + a supervised model's probability + an isolation-forest anomaly score blend into one explainable risk score per transaction (`config.yaml` `risk.blend`). |
| 🧠 | **Agentic reasoning (optional)** — a bounded Anthropic Claude tool-use loop answers questions in plain English, **only from tool outputs, never from invented numbers**; an activity log proves every claim. |
| ⚡ | **Outbound risk-alert webhook (opt-in)** — flip `features.webhook_alerts` + set `alerts.webhook_url`, and a live risk scan that flags a transaction above the threshold POSTs a small JSON payload to your endpoint (Slack Incoming Webhook or any HTTP receiver); deduplicated per transaction so repeated scans never spam. |
| 📴 | **Offline narrator fallback** — no `ANTHROPIC_API_KEY`? The agent degrades to a deterministic narrator that answers the same questions. Zero credentials required. |
| 📊 | **Honest benchmarking** — 6 fraud-detection models compared by **mean PR-AUC over 5-fold time-series CV** (temporal split, strictly backward-looking features, no leakage). |
| 🕵️ | **Per-transaction explanations** — native LightGBM TreeSHAP shows exactly *why* any flagged transaction scored the way it did (contributions + bias = log-odds). |

## 🏗️ Architecture

```text
FinSight Agent/
├── app/
│   ├── synthetic/              # Deterministic ledger generator (3 tiers)
│   ├── features/               # Feature engineering + fraud patterns
│   ├── models/                 # 6 benchmarked models + TreeSHAP
│   ├── agent/                  # Tool-use loop + narrator (LLM + offline)
│   ├── api/                    # FastAPI facts endpoint
│   └── app/                    # Streamlit dashboard
├── config.yaml
├── requirements.txt
└── README.md
```

The deterministic path runs with zero credentials. The LLM path is optional and wired behind feature flags.

## 🚀 Quick Start

### Prerequisites

- Python 3.10 or newer
- `pip` (or a virtual environment of your choice)

### Install & run

```bash
# 1. Clone the repository
git clone https://github.com/themanoj-025/FinSight.git
cd FinSight

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Generate a small synthetic ledger (tiny tier, for CI/demo)
python -m app.synthetic.generator --tier tiny --out data/synthetic_tiny.csv

# 5. Train and benchmark the 6 models
python -m app.models.benchmark --cv-folds 5 --out results/benchmark.json

# 6. Run the Streamlit dashboard
streamlit run app/streamlit_app.py
```

### Optional: LLM-driven narration

Set these environment variables to enable the Claude tool-use narrator and the outbound webhook:

| Variable | Default | Required | Description |
| -------- | ------- | -------- | ----------- |
| `ANTHROPIC_API_KEY` | — | No | Enables the agentic narrator (LLM path). Falls back to offline narrator when unset. |
| `ALERTS_WEBHOOK_URL` | — | No | Endpoint that receives a deduplicated risk-alert JSON payload on each flagged transaction. |

> [!NOTE] No LLM key is required. The app runs fully offline; features above are opt-in.

## 📁 Project structure

```
finsight-agent/
├── app/
│   ├── synthetic/              # Deterministic ledger generator
│   ├── features/               # Feature engineering + fraud patterns
│   ├── models/                 # 6 models + TreeSHAP
│   ├── agent/                  # Tool-use loop + narrator
│   ├── api/                    # FastAPI facts endpoint
│   └── streamlit_app.py        # Dashboard
├── model_bench/                # Benchmark reproduction
├── config.yaml
├── requirements.txt
└── README.md
```

## 🧪 Testing

```bash
# Run the full suite
pytest -q

# Reproduce the benchmark (6 models, 5-fold time-series CV)
python -m app.models.benchmark --cv-folds 5 --out results/benchmark.json
```

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md).

## 📄 License

MIT License — see [LICENSE](LICENSE).
