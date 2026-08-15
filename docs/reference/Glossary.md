# Glossary — FinSight Agent: Shared Vocabulary

| Field | Value |
| --- | --- |
| Version | v0.2 |
| Last Updated | 2026-08-07 |
| Owner | Tech Writer |
| Status | Approved |

---

| Term | Definition |
| --- | --- |
| Facts layer | Deterministic data/rules/models/tools |
| Reasoning layer | Agent loop / narrator |
| Blended risk score | `w_rules·rule + w_model·model + w_iforest·iforest`, weights read live from `config.yaml risk.blend` |
| PR-AUC | Precision-Recall AUC — model selection metric |
| Tool call | A facts-layer function the agent invokes |
| Activity log | Record of every tool call (proves grounding) |
| Narrator | Offline deterministic answerer |
| Ledger | Synthetic transaction dataset |
| Anomaly | Isolation-forest flagged outlier |
| Audit rule | Hand-written detector (balance drain etc.) |
| Focal user | The single tracked account holder |
| Service mode | The app running as a client of the facts HTTP API (`FINSIGHT_API_URL` set) |
| ApiClient | Stdlib-only HTTP client mirroring the facts interface (`app/api_client.py`) |
| SQLite store | Optional persistence layer with a materialized `risk_scores` table (`finance_agent/storage.py`) |
| SHAP (TreeSHAP) | Per-transaction feature-contribution explanation (LightGBM `pred_contrib`) |
| Renormalization | Rule-only blend fallback: the risk score collapses to the rule score (weight 1.0) when no model bundle exists |

## Related Documents

| Document | Relationship |
| --- | --- |
| [PRD.md](../product/PRD.md) | Terms used there |
| [TechSpec.md](../technical/TechSpec.md) | Terms used there |
| [AppFlow.md](../design/AppFlow.md) | Terms used there |
| [Design.md](../design/Design.md) | Terms used there |
| [Schema.md](../technical/Schema.md) | Terms used there |
| [ImplementationPlan.md](../project/ImplementationPlan.md) | Terms used there |
| [Tracker.md](../project/Tracker.md) | Terms used there |
| [Rules.md](../project/Rules.md) | Terms used there |
| [API.md](../technical/API.md) | Terms used there |
| [SecurityAndCompliance.md](../technical/SecurityAndCompliance.md) | Terms used there |
| [Testing.md](../technical/Testing.md) | Terms used there |
| [Deployment.md](../technical/Deployment.md) | Terms used there |
| [RiskRegister.md](../project/RiskRegister.md) | Terms used there |
