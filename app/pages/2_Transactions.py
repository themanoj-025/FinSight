"""Transactions Explorer — filter, sort, search, and export the ledger."""

from typing import cast

import pandas as pd
import streamlit as st

from app import common


def _load() -> pd.DataFrame:
    facts = common.get_facts()
    df = facts.df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime", ascending=False).reset_index(drop=True)


def render() -> None:
    st.set_page_config(
        page_title="Transactions · FinSight Agent",
        page_icon="🧾",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    common.inject_css()
    common.require_auth()
    common.ensure_data()

    df = _load()
    common.page_header("Transactions Explorer", "Filter, search, and export the full ledger")

    # v2 columns (subcategory / region / account) may be absent on legacy data.
    has_v2 = all(c in df.columns for c in ("subcategory", "account_type", "transaction_region"))

    with st.expander("Filters", expanded=True):
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            categories = sorted(df["category"].dropna().unique().tolist())
            cats = st.multiselect("Category", categories, default=categories)
        with f2:
            types = sorted(df["type"].dropna().unique().tolist())
            types_sel = st.multiselect("Type", types, default=types)
        with f3:
            # date_input expects datetime.date objects — convert the ISO strings
            # explicitly instead of relying on undocumented string tolerance.
            dates = pd.to_datetime(df["date"]).dt.date
            value = st.date_input("Date range", value=(dates.min(), dates.max()))
            if isinstance(value, tuple) and len(value) == 2:
                # Range picker: streamlit returns (start, end) when the default
                # is a 2-tuple; the stub over-approximates with empty/single
                # tuples, so narrow by length and index explicitly.
                dmin, dmax = cast("tuple[object, object]", value)
            else:
                dmin, dmax = value, value
        with f4:
            amt_min, amt_max = st.slider(
                "Amount range ($)", 0.0, float(df["amount"].max()), (0.0, float(df["amount"].max()))
            )
        if has_v2:
            g1, g2, g3 = st.columns(3)
            with g1:
                accts = sorted(df["account_type"].dropna().unique().tolist())
                acct_sel = st.multiselect("Account type", accts, default=accts)
            with g2:
                regions = sorted(df["transaction_region"].dropna().unique().tolist())
                region_sel = st.multiselect("Region", regions, default=regions)
            with g3:
                subs = sorted(df["subcategory"].dropna().unique().tolist())
                sub_sel = st.multiselect("Subcategory", subs, default=subs)
        search = st.text_input(
            "Search merchant / account", placeholder="e.g. Netflix, Whole Foods, U_Alex"
        )
        focal_only = st.toggle("Focal user only", value=False)

    mask = df["category"].isin(cats) & df["type"].isin(types_sel)
    mask &= df["date"].between(str(dmin), str(dmax))
    mask &= df["amount"].between(amt_min, amt_max)
    if has_v2:
        mask &= df["account_type"].isin(acct_sel)
        mask &= df["transaction_region"].isin(region_sel)
        mask &= df["subcategory"].isin(sub_sel)
    if search.strip():
        mask &= df["merchant"].astype(str).str.contains(search, case=False, na=False) | df[
            "nameOrig"
        ].astype(str).str.contains(search, case=False, na=False)
    if focal_only:
        mask &= df["is_focal_user"]
    view = df[mask]

    spending_view = view.loc[~view["type"].isin(["SALARY", "CASH_IN"])]
    st.markdown(
        f"**{len(view):,}** transactions · total "
        f"**${view['amount'].sum():,.2f}** (spending "
        f"${spending_view['amount'].sum():,.2f})"
    )
    cols = [
        "datetime",
        "date",
        "type",
        "amount",
        "merchant",
        "category",
        "nameOrig",
        "nameDest",
        "is_focal_user",
        "isFraud",
        "is_anomaly",
    ]
    if has_v2:
        cols = [
            "datetime",
            "date",
            "type",
            "amount",
            "merchant",
            "category",
            "subcategory",
            "account_type",
            "transaction_region",
            "nameOrig",
            "nameDest",
            "is_focal_user",
            "isFraud",
            "is_anomaly",
        ]
    st.dataframe(
        view[cols],
        width="stretch",
        column_config={
            "amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
            "isFraud": st.column_config.CheckboxColumn("Fraud"),
            "is_anomaly": st.column_config.CheckboxColumn("Anomaly"),
            "is_focal_user": st.column_config.CheckboxColumn("Focal"),
        },
        hide_index=True,
    )
    csv = view.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download filtered CSV", data=csv, file_name="finsight_transactions.csv", mime="text/csv"
    )


if __name__ == "__main__":
    common.run_render(render)
