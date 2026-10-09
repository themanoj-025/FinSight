"""FinSight Agent — an autonomous personal finance analyst.

Layers (strictly separated):
  * Facts layer:     finance_agent.tools / .rules / .features / model_bench
  * Reasoning layer: finance_agent.agent (LLM tool-use loop + offline fallback)
  * Presentation:    app/ (Streamlit), finance_agent.cli, finance_agent.report
"""

__version__ = "0.1.0"
