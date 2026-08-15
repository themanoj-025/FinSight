# FinSight Agent — 90-second demo script (G.2)

> Target: **60–90 seconds**, one continuous screen recording, no cuts required
> (each shot is a slow pan/scroll from the previous one). Record at 1080p.
> Post-production: trim, add the caption bar, export to GIF/MP4, then embed at
> the top of the README next to the live URL.

## Pre-flight checklist

- [ ] `make all` (data + model) ran; the app is up (`make run` or the live URL).
- [ ] Ask the Agent page: either `ANTHROPIC_API_KEY` set locally, or stay in
      offline-narrator mode (the demo does not depend on the LLM path).
- [ ] Screen at 1080p, cursor hidden (or kept slow and deliberate).
- [ ] Browser zoom 100%; dark theme (the app's default).

## Shot list

| Time | Screen | Say / show |
|---|---|---|
| 0:00–0:12 | Dashboard | KPI cards (income / savings / expenses), category donut, monthly trend. Narration: *"Here's a month of a gig-worker persona's finances — income, savings rate, where the money actually goes."* |
| 0:12–0:35 | Fraud Detection | Scroll to a flagged transaction, click it → **SHAP bar chart** (why it's flagged) and the **similar-transactions retrieval panel** side by side. Narration: *"This card is flagged. The SHAP bars tell us why — and next to it, the three most similar transactions in the ledger, including one from a known fraud archetype. This is *why*, not just a score."* |
| 0:35–0:55 | Ask the Agent | Type one real question (e.g. *"What did I spend on dining last month, and is that up?"*), show the streamed answer, then expand the **activity log** at the bottom. Narration: *"Every number the agent just said traces to one of these tool calls — the facts endpoint, the spend-spikes tool. It never invents figures."* |
| 0:55–1:15 | Trust & Transparency | Scroll the model card summary: algorithm, CV vs holdout metrics, **per-archetype recall** (call out the honest adversarial-tier gap explicitly: *"refund-abuse and subscription-creep recall is near zero — that's a real, disclosed limitation, not a marketing number"*), cohort fairness, calibration. |
| 1:15–1:25 | Close | Back to the Dashboard or the live URL in the address bar. Narration: *"FinSight Agent — link in the README. Offline by default, bring-your-own-key for the LLM."* |

## Recording tips

- **0:12–0:35 is the money shot** — make sure the click → SHAP + similar panel
  transition is on camera and unhurried (the page does real work; let it finish).
- If the LLM key isn't set, the Ask the Agent page shows the BYOK banner —
  that's *fine* and worth one line: *"No API key needed — this runs in
  deterministic offline mode."* (It demonstrates the honest-by-default design.)
- Keep the narration to ~90 words total; the captions carry the rest.

## Where it goes

1. Render → `docs/assets/finsight_demo.mp4` (and/or `.gif`).
2. Commit + push, then embed at the top of the README:
   ```md
   [![FinSight Agent demo](docs/assets/finsight_demo.gif)](docs/assets/finsight_demo.mp4)
   ```
   right above the live-URL line from §3b.
