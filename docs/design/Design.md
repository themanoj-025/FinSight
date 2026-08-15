# Design — FinSight Agent: Design System & UX Principles

| Field | Value |
| --- | --- |
| Version | v0.2 |
| Last Updated | 2026-08-07 |
| Owner | Design Lead |
| Status | Approved |

---

## 1. Design Principles

1. **Trust through transparency** — every chart has a source; every answer shows its tools.
2. **Decision-ready dashboards** — KPIs + callouts at a glance.
3. **Calm data density** — tables and charts, minimal prose.
4. **Consistent** — shared components across 7 pages.
5. **Offline-honest** — narrator vs LLM output is labeled.

## 2. Brand & Visual Identity

- Voice: analytical, trustworthy, plain-English.
- Imagery: charts, donuts, KPI cards; no decoration.

## 3. Color System

| Token | Hex | Usage | Contrast (AA) |
| --- | --- | --- | --- |
| bg | `#F8FAFC` | light bg | — |
| surface | `#FFFFFF` | cards | — |
| text | `#0F172A` | body | 15:1 |
| primary | `#2563EB` | CTAs | 5.9:1 |
| risk-high | `#DC2626` | fraud flags | 5.9:1 |
| risk-medium | `#D97706` | anomalies | 4.7:1 |
| ok | `#16A34A` | healthy | 5.1:1 |
| muted | `#64748B` | secondary | 4.9:1 |

## 4. Typography Scale

| Token | Font | Size | Weight | Line-height | Usage |
| --- | --- | --- | --- | --- | --- |
| display | system sans | 30px | 700 | 1.2 | KPI numbers |
| heading | system sans | 20px | 600 | 1.3 | page titles |
| body | system sans | 14px | 400 | 1.5 | content |
| table | mono | 13px | 400 | 1.4 | transactions |
| label | system sans | 12px | 600 | 1.4 | field labels |

## 5. Spacing & Grid

- Base 4px; Streamlit layout.
- Breakpoints: Streamlit responsive.

## 6. Component Library

**KPI card:**

```
┌───────────────────┐
│ Balance  1,200.00  │
│ ▲ +4.2% this month │
└───────────────────┘
```

**Risk flag:**

```
⚠ HIGH RISK — 45,000 transfer at 2 AM
  Reasons: balance drain · new payee
```

Other: category donut, trend chart, transaction table, chat panel + activity log sidebar, model comparison charts, report preview.

## 7. Iconography

Plotly + Unicode; no image assets.

## 8. Accessibility

- WCAG 2.1 AA targets.
- Risk never color-only.
- Keyboard nav.

## 9. Responsive

- Fluid dashboard; tables scroll.

## 10. Motion

- Chart transitions (300ms); chat streaming cursor; reduced-motion honored.

## 11. Dark Mode

Light theme; dark roadmap.

## 12. Related Documents

| Document | Relationship |
| --- | --- |
| [AppFlow.md](AppFlow.md) | Screens |
| [PRD.md](../product/PRD.md) | UX goals |
| [TechSpec.md](../technical/TechSpec.md) | Stack |
| [Schema.md](../technical/Schema.md) | Data |
| [ImplementationPlan.md](../project/ImplementationPlan.md) | Tasks |
| [Tracker.md](../project/Tracker.md) | Status |
| [Rules.md](../project/Rules.md) | Standards |
| [API.md](../technical/API.md) | Contracts |
| [SecurityAndCompliance.md](../technical/SecurityAndCompliance.md) | Data |
| [Testing.md](../technical/Testing.md) | UI tests |
| [Deployment.md](../technical/Deployment.md) | Deploy |
| [Glossary.md](../reference/Glossary.md) | Vocabulary |
| [RiskRegister.md](../project/RiskRegister.md) | Risks |
