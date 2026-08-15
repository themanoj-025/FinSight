"""Command-line interface — `ask`, `chat`, `report`, and `digest` subcommands.

Examples:
    python -m finance_agent ask "Any suspicious activity?"
    python -m finance_agent chat
    python -m finance_agent report --out reports/monthly_report.md
    python -m finance_agent digest --out reports/weekly_digest.md
"""

from __future__ import annotations

import argparse
import logging

from finance_agent.agent import FinanceAgent
from finance_agent.digest import run_digest
from finance_agent.report import write_report


def _run_ask(agent: FinanceAgent, question: str) -> None:
    print(agent.answer(question))


def _run_chat(agent: FinanceAgent) -> None:
    print("FinSight Agent chat — type a question, Ctrl-C / Ctrl-D to exit.")
    print(
        "(offline narrator)"
        if not agent.llm_available()
        else f"(LLM: {agent.agent_cfg.get('model')})"
    )
    print("=" * 60)
    history: list[dict[str, str]] = []
    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            return
        if not question:
            continue
        print("\nFinSight:", end=" ")
        reply = str(agent.answer(question, history=history))
        print(reply)
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": reply})
        history = history[-20:]  # cap conversation growth


def main(argv: list[str] | None = None) -> int:
    # D.1: structured JSON logging for CLI sessions (correlation ids come from
    # `finsight chat` sessions via finance_agent/observability.py).
    from finance_agent.observability import configure_logging

    configure_logging(logging.WARNING)
    parser = argparse.ArgumentParser(
        prog="finsight", description="FinSight Agent — autonomous personal finance analyst."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="Ask the agent a single question.")
    ask.add_argument("question", nargs="+", help='The question, e.g. "Any suspicious activity?"')

    sub.add_parser("chat", help="Start an interactive chat session.")

    rep = sub.add_parser("report", help="Generate the monthly Markdown report (optionally as PDF).")
    rep.add_argument("--out", default=None, help="Output path (default reports/monthly_report.md)")
    rep.add_argument(
        "--pdf",
        action="store_true",
        help="Also write a branded PDF (reports/monthly_report.pdf) next to the Markdown.",
    )

    dig = sub.add_parser(
        "digest", help="Build + deliver the weekly digest (Slack/email if configured)."
    )
    dig.add_argument("--out", default=None, help="Output path (default reports/weekly_digest.md)")

    args = parser.parse_args(argv)
    agent = FinanceAgent()
    if args.command == "ask":
        _run_ask(agent, " ".join(args.question))
    elif args.command == "chat":
        _run_chat(agent)
    elif args.command == "report":
        path = write_report(args.out)
        print(f"Report written to {path}")
        if args.pdf:
            from pathlib import Path

            from finance_agent.pdf_export import write_report_pdf

            out_dir = Path(args.out).parent if args.out else Path("reports")
            pdf_path = write_report_pdf(str(out_dir / "monthly_report.pdf"), facts=agent.facts)
            print(f"PDF written to {pdf_path}")
    elif args.command == "digest":
        md = run_digest(out_path=args.out)
        print(f"Weekly digest built ({len(md)} chars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
