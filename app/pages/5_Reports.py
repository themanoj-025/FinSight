"""Reports — generate, preview, and download the full monthly report."""

import streamlit as st

from app import common


def render() -> None:
    st.set_page_config(
        page_title="Reports · FinSight Agent",
        page_icon="📄",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()

    common.page_header(
        "Reports", "A self-contained Markdown digest — every figure comes from a tool output"
    )

    if st.button("⚡ Regenerate report", type="primary"):
        st.session_state.report_content = _generate()

    if "report_content" not in st.session_state:
        st.session_state.report_content = _generate()

    content = st.session_state.report_content
    st.markdown(content)
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "⬇️ Download as Markdown",
            data=content.encode("utf-8"),
            file_name="finsight_monthly_report.md",
            mime="text/markdown",
            type="primary",
        )
    with c2:
        st.download_button(
            "📄 Download as branded PDF",
            data=_pdf_bytes(content),
            file_name="finsight_monthly_report.pdf",
            mime="application/pdf",
        )
    st.caption(
        "The PDF is generated in-process by a hand-rolled, stdlib-only writer — "
        "no extra dependencies, works fully offline."
    )


def _generate() -> str:
    from finance_agent.report import build_report

    # Use the same data source as every other page (the FinSight API when
    # FINSIGHT_API_URL is set, local facts otherwise). build_report duck-types
    # either — ApiClient mirrors the FinanceFacts interface.
    facts = common.get_facts()
    return build_report(facts)


def _pdf_bytes(markdown: str) -> bytes:
    from finance_agent.pdf_export import build_report_pdf

    return build_report_pdf(markdown)


if __name__ == "__main__":
    common.run_render(render)
