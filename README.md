<div align="center">

<p>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg" />
    <img src="assets/logo.svg" width="280" alt="FinSight Agent logo — candlestick tile and wordmark" />
  </picture>
</p>

<h1>FinSight Agent</h1>

<p>
  <strong>Autonomous personal finance analysis: turns raw transactions into fraud alerts, spending insights, and explainable advice.</strong>
</p>

<p>
  <a href="https://github.com/themanoj-025/FinSight/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/themanoj-025/FinSight/ci.yml?style=flat-square&label=CI" alt="CI Status" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License MIT" /></a>
  <a href="https://github.com/themanoj-025/FinSight/stargazers"><img src="https://img.shields.io/github/stars/themanoj-025/FinSight?style=social" alt="GitHub Stars" /></a>
  <a href="https://github.com/themanoj-025/FinSight/issues"><img src="https://img.shields.io/github/issues/themanoj-025/FinSight?style=flat-square" alt="GitHub Issues" /></a>
  <a href="https://github.com/themanoj-025/FinSight/pulls"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square" alt="PRs Welcome" /></a>
</p>

</div>

---

## Overview

FinSight Agent provides an end-to-end, agentic financial analytics system. It transforms raw bank statements into risk-scored fraud warnings, categorized spending analytics, and natural-language financial recommendations.

The architecture combines deterministic accounting validation rules, supervised gradient boosting (LightGBM with TreeSHAP explanations), and an optional LLM tool-use reasoning loop (powered by Anthropic Claude or local offline narrators).

> [!NOTE]
> FinSight Agent operates completely offline by default with zero external API credentials. External LLM capabilities (`ANTHROPIC_API_KEY`) and alerting webhooks (`ALERTS_WEBHOOK_URL`) are opt-in enhancements.

---

## Table of Contents

- [Key Features](#key-features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation & Quick Start](#installation--quick-start)
- [Configuration](#configuration)
- [Model Benchmarking & Reproducibility](#model-benchmarking--reproducibility)
- [Known Limitations](#known-limitations)
- [Testing](#testing)
- [Contributing](#contributing)
- [Security](#security)
- [License](#license)
- [Support](#support)

---

## Key Features

- **Synthetic Ledger Generator**: Vectorized generator producing realistic transactions across 6 financial archetypes with multi-account structures and 15 distinct fraud injection patterns.
- **Hybrid Risk Scoring Engine**: Fuses rule-based accounting guards, supervised classification probabilities, and isolation forest anomaly metrics into a single calibrated score.
- **TreeSHAP Explainability**: Native LightGBM TreeSHAP attribution exposes the exact positive and negative feature contributions driving every fraud alert.
- **Agentic Financial Narrator**: Autonomous tool-calling reasoning agent that reads strictly from verified tool outputs without inventing financial metrics.
- **Real-Time Webhook Dispatch**: Optional outbound HTTP webhook that delivers deduplicated JSON payloads to Slack or incident management systems upon alert triggers.

---

## Architecture

```text
       Raw Transaction Stream / Synthetic Generator
                            │
                            ▼
               Feature Engineering Engine
         (Temporal Split & Anti-Leakage Pipeline)
                            │
                            ▼
                 Hybrid Risk Scorer
     ┌──────────────────────┼──────────────────────┐
     ▼                      ▼                      ▼
Deterministic Rules    LightGBM Model       Isolation Forest
(Audit Constraints)    (PR-AUC Tuned)       (Anomaly Detector)
     │                      │                      │
     └──────────────────────┼──────────────────────┘
                            │
                            ▼
                 Calibrated Risk Score
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
   TreeSHAP Attributions           Agentic Narrator
   (Feature Drivers)               (Claude / Offline)
            │                               │
            └───────────────┬───────────────┘
                            ▼
              Streamlit Analytics Interface
              (app/streamlit_app.py)
```

---

## Project Structure

```text
finsight-agent/
├── app/                    # Streamlit dashboards and visualization components
├── finance_agent/          # Core package: synthetic generator, models, agents
├── model_bench/            # Model benchmark suites and evaluation scripts
├── tests/                  # Comprehensive pytest test suite
├── config.yaml             # Application configuration, thresholds, and weights
├── Dockerfile              # Container image build specification
├── docker-compose.yml      # Local multi-service composition
├── pyproject.toml          # Build configuration and dependency declarations
├── requirements.lock       # Pinned deterministic dependencies
└── README.md               # Project documentation
```

---

## Prerequisites

- **Python**: `>= 3.11` (Python 3.11 or 3.12 recommended)
- **Anthropic API Key**: Optional, required only for agentic Claude narration

---

## Installation & Quick Start

### 1. Clone & Setup Environment

```bash
# Clone the repository
git clone https://github.com/themanoj-025/FinSight.git
cd FinSight

# Create virtual environment
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.lock
```

### 2. Generate Synthetic Financial Ledger

```bash
# Generate deterministic transactions (tiny tier for quick demo)
python -m finance_agent.synthetic.generator --tier tiny --out data/synthetic_tiny.csv
```

### 3. Launch Streamlit Application

```bash
streamlit run app/streamlit_app.py
```
*Open http://localhost:8501 in your browser.*

---

## Configuration

Settings are declared in `config.yaml` and can be overridden via environment variables:

| Setting / Variable | Default | Required | Purpose |
| :--- | :--- | :--- | :--- |
| `ANTHROPIC_API_KEY` | `""` | No | Enables the LLM tool-calling agent (falls back to offline narrator when unset) |
| `ALERTS_WEBHOOK_URL` | `""` | No | Target HTTP endpoint for outbound risk alerts |
| `risk.blend.rules_weight` | `0.4` | No | Relative weight for deterministic audit rules |
| `risk.blend.ml_weight` | `0.4` | No | Relative weight for supervised model probability |
| `risk.blend.anomaly_weight` | `0.2` | No | Relative weight for unsupervised isolation forest score |

---

## Model Benchmarking & Reproducibility

FinSight evaluates models using strictly temporal, 5-fold cross-validation without lookahead leakage.

```bash
# Run the 6-model cross-validation benchmark
python -m model_bench.benchmark --cv-folds 5 --out reports/benchmark.json
```

Benchmark results from the synthetic validation suite:
- **Baseline LightGBM CV PR-AUC**: `0.828 ± 0.055`
- Full evaluation reports and metric curves are preserved in `reports/`.

---

## Known Limitations

- **Vector Search Scale**: Transaction similarity search utilizes an in-memory index; high-volume production deployments (> 1M records) should use pgvector or dedicated vector databases.
- **Synthetic Distribution**: Although the synthetic generator covers 15 adversarial attack patterns, production fraud distributions exhibit evolving real-world drift.
- **LLM Token Costs**: On-demand LLM narrative generation incurs API costs when processing high-frequency transaction volumes; use the deterministic offline narrator for batch workflows.

---

## Testing

```bash
# Execute standard pytest test suite
pytest tests/ -v

# Run with coverage analysis
pytest --cov=finance_agent tests/
```

---

## Contributing

Contributions are welcomed! Please see [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for workflow guidelines.

---

## Security

Please report vulnerabilities responsibly in accordance with [SECURITY.md](SECURITY.md).

---

## License

This project is licensed under the [MIT License](LICENSE).

---

## Support

- 🐛 **Issue Tracker**: [GitHub Issues](https://github.com/themanoj-025/FinSight/issues)
- 💡 **Discussions & Feedback**: [GitHub Pull Requests](https://github.com/themanoj-025/FinSight/pulls)
