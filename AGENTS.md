# AGENTS.md — finsight-agent

> Canonical project instructions. Pointers like `CLAUDE.md` or
> `.github/copilot-instructions.md` should say "See AGENTS.md".

---

## Project overview

**finsight-agent** — a fintech insight generation agent. Core
components:

- **Data** — raw + processed financial data (DVC-tracked).
- **Pipeline** — feature extraction and insight generation jobs.
- **Model** — intent/insight classifier and summarizer.
- **API** — FastAPI service exposing the agent.
- **Streamlit** — dashboard for reviewing generated insights.

Stack: Python 3.11+ · FastAPI · pandas · scikit-learn · Streamlit · DVC.

---

## Exact commands

```bash
# Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Data (DVC)
dvc pull

# Lint / typecheck / test
make lint
pre-commit run --all-files
python -m mypy . --ignore-missing-imports
python -m pytest tests/ -v --cov=. --cov-fail-under=70

# Run
uvicorn api.main:app --reload
streamlit run dashboard/app.py
```

---

## Folder map

| Path | Purpose |
|------|---------|
| `data/` | Data (DVC-tracked) |
| `src/` | Pipeline + model code |
| `api/` | FastAPI application |
| `dashboard/` | Streamlit app |
| `tests/` | pytest suite |
| `.github/workflows/` | CI (ruff, mypy, pytest, gitleaks, trivy) |

## Do / don't

- **Do** keep financial data masks/PINs tokenized before any file leaves
  the sandbox.
- **Do not** commit `.dvc/config` (it holds remote pointers).
- **Do not** commit `.env` files.

## Security rules

- No secrets in the repository; `gitleaks` CI gate gates on hits.
- Card numbers / PANs must be masked or tokenized before any file
  leaves the sandbox.

## AI-assistance convention

Commits authored by AI must carry the trailer:

```text
AI-Assisted: yes | no | partial
```

See `.gitmessage` for the template. Do not rewrite historic commits
retroactively.
