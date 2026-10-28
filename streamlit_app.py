"""DMC Competitor Finder - run with:  streamlit run streamlit_app.py  (single process, no separate backend)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from app.bootstrap import get_app  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.models import AnalysisOptions, Component, ReferencePackage  # noqa: E402
from ui.auth import logout_button, require_login  # noqa: E402

RUNNING = {"uploaded", "extracting", "queued", "searching", "verifying", "matching"}
LEVEL_COLORS = {"same": "#2E7D32", "near": "#1565C0", "partial": "#F9A825", "rejected": "#9E9E9E"}
LEVEL_HELP = {
    "same": "Every component of your package is proven on the competitor's page.",
    "near": "Most components proven (70%+), same length and class, nothing excluded.",
    "partial": "A whole package with a price, but some components are missing/unproven or length/class differs.",
    "rejected": "Not a comparable package (single activity, hotel/flight only, other destination).",
}

st.set_page_config(page_title="DMC Competitor Finder", page_icon="🧭", layout="wide")
st.markdown(
    """<style>
    .block-container {padding-top: 4rem; max-width: 1500px;}
    div[data-testid="stMetric"] {background: rgba(128,128,128,0.08); border-radius: 10px; padding: 10px 14px;}
    </style>""",
    unsafe_allow_html=True,
)

_FAILED = object()


def guard(fn, *a, **k):
    """Run an action and show a friendly message instead of a stack trace. Returns _FAILED on error."""
    try:
        r = fn(*a, **k)
        return True if r is None else r
    except AppError as e:
        st.error(e.message)
    except Exception as e:
        st.error(f"Unexpected error: {type(e).__name__}: {e}")
    return _FAILED


def md(text: str) -> str:
    """Escape $ so Streamlit markdown does not treat prices as LaTeX."""
    return text.replace("$", "\\$")


def signed_money(x) -> str:
    return "" if x is None or pd.isna(x) else f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def money(x, digits=0):
    return "n/a" if x is None or (isinstance(x, float) and pd.isna(x)) else f"${x:,.{digits}f}"


def set_current(aid):
    if aid:
        st.session_state["aid"] = aid
        st.query_params["id"] = aid
    else:
        st.session_state.pop("aid", None)
        st.query_params.clear()


# ----------------------------------------------------------------------------- sidebar
def sidebar(app) -> None:
    st.sidebar.title("🧭 DMC Competitor Finder")
    problems = app.problems()
    if problems:
        st.sidebar.error("Needs setup:\n\n" + "\n".join(f"• {p}" for p in problems), icon="⚠️")
    else:
        s = app.settings
        st.sidebar.success(f"Ready · OpenAI ({s.openai_model}) · search: {', '.join(s.search_engines())}", icon="✅")
    if st.sidebar.button("➕ New analysis", width="stretch", type="primary"):
        set_current(None)
        st.rerun()
    st.sidebar.markdown("**History**")
    items = app.list_analyses()
    if not items:
        st.sidebar.caption("No analyses yet.")
    icons = {"completed": "✅", "failed": "❌", "awaiting_review": "📝"}
    for it in items[:30]:
        title = ""
        if it.get("reference_json"):
            try:
                import json

                title = json.loads(it["reference_json"]).get("title") or ""
            except ValueError:
                pass
        label = f"{icons.get(it['status'], '⏳')} {title or it['image_name']} · {it['created_at'][5:16].replace('T', ' ')}"
        if st.sidebar.button(label, key=f"h_{it['id']}", width="stretch"):
            set_current(it["id"])
            st.rerun()
    logout_button()


# ----------------------------------------------------------------------------- new analysis
def page_new(app) -> None:
    st.title("Find who sells the same package as yours")
    st.write(
        "Open your package page, capture it with **GoFullPage** (PNG), and upload it here. Works for any package and destination. "
        "The system reads every detail, searches the internet for sellers of the **same or closest** package, checks each page, "
        "and shows who sells it for how much - even when a competitor is missing some of your inclusions."
    )
    left, right = st.columns([3, 2])
    with left:
        f = st.file_uploader("Full-page screenshot (PNG, JPG, WEBP)", type=["png", "jpg", "jpeg", "webp"])
        if f is not None:
            st.image(f, caption=f.name, width=420)
    with right:
        review = st.checkbox("Let me review the extracted details before searching", value=True,
                             help="Recommended: fix any misread value or component first. Matching is only as good as this list.")
        depth = st.selectbox("Search depth", ["standard", "fast", "deep"],
                             help="fast ≈ 25 pages · standard ≈ 45 pages + linked pages · deep ≈ 80 pages + linked pages, more regions")
        strict = st.selectbox("Strictness", ["standard", "strict", "relaxed"],
                              help="standard: exact + closest matches (70%+) drive the statistics · strict: exact packages only · relaxed: 50%+")
        with st.expander("Advanced options"):
            max_c = st.slider("Max pages to verify (0 = automatic by depth)", 0, 150, 0, step=5)
            max_q = st.slider("Max search queries (0 = automatic by depth)", 0, 30, 0)
            flights = st.checkbox("Include flight-bundled packages in price statistics", value=False)
            extra = st.text_area("Extra search queries (one per line)", placeholder="china 8 days 7 nights beijing shanghai package")
    if st.button("🚀 Analyse screenshot", type="primary", disabled=f is None):
        opts = AnalysisOptions(review_before_search=review, depth=depth, strictness=strict,
                               max_candidates=max_c or None, max_queries=max_q or None,
                               include_flight_packages_in_stats=flights,
                               extra_queries=[x.strip() for x in extra.splitlines() if x.strip()])
        with st.spinner("Uploading…"):
            aid = guard(app.create_analysis, f.name, f.getvalue(), opts)
        if aid is not _FAILED:
            set_current(aid)
            st.rerun()


# ----------------------------------------------------------------------------- running
@st.fragment(run_every=2)
def live_status(aid: str, seen_status: str) -> None:
    app = get_app()
    a = app.get(aid)
    if a["status"] != seen_status:
        st.rerun(scope="app")
    st.progress(min(max(a["progress"], 0), 100) / 100, text=md(a["message"] or a["status"]))
    with st.expander("Live log", expanded=False):
        st.code("\n".join(f"{e['ts'][11:19]}  {e['message']}" for e in app.events(aid)[-40:]) or "…", language=None)


def page_running(app, aid: str, d: dict) -> None:
    st.info("The analysis is running. You can leave this page open; it updates automatically. Typical time: 2–6 minutes.")
    live_status(aid, d["status"])
    if st.button("Cancel analysis"):
        guard(app.cancel, aid)


# ----------------------------------------------------------------------------- review
def page_review(app, aid: str, d: dict) -> None:
    ref = ReferencePackage(**d["reference"])
    st.subheader("Step 2 · Check what was read from your screenshot")
    st.caption("Matching uses the component list below. Fix anything wrong, add missing items, delete irrelevant ones. "
               "'Match rule' (optional) tells the checker what a competitor page must show for that item.")
    c1, c2 = st.columns([2, 3])
    with c1:
        img = app.image_preview(aid, 700)
        if img:
            st.image(img, caption="Your screenshot")
        else:
            st.caption("Preview unavailable.")
    with c2:
        a, b = st.columns(2)
        title = a.text_input("Package title", ref.title)
        dest = b.text_input("Destination (country / route)", ref.destination)
        cities = st.text_input("Cities (comma separated)", ", ".join(ref.cities))
        n1, n2, n3, n4 = st.columns(4)
        nights = n1.number_input("Nights", 0, 60, int(ref.nights or 0))
        days = n2.number_input("Days", 0, 61, int(ref.days or 0))
        star = n3.selectbox("Hotel stars", [0, 1, 2, 3, 4, 5], index=int(ref.hotel_star or 0), format_func=lambda x: "n/a" if x == 0 else f"{x}★")
        transfers = n4.selectbox("Transfers", ["", "SIC", "private"], index=["", "SIC", "private"].index(ref.transfers or ""),
                                 format_func=lambda x: x or "not specified")
        p1, p2, p3, p4 = st.columns(4)
        price = p1.number_input("Your price", 0.0, 10_000_000.0, float(ref.price_amount or 0), step=1.0)
        cur = p2.text_input("Currency", ref.price_currency, max_chars=3)
        basis = p3.selectbox("Price basis", ["per person", "per couple", "per group"],
                             index=["per person", "per couple", "per group"].index(ref.price_basis if ref.price_basis in ("per person", "per couple", "per group") else "per person"))
        flights = p4.selectbox("Flights included", ["No", "Yes"], index=1 if ref.flights_included else 0)
        comps_df = pd.DataFrame([{"name": c.name, "category": c.category, "required": c.required, "match_rule": c.match_rule or ""}
                                 for c in ref.components], columns=["name", "category", "required", "match_rule"])
        edited = st.data_editor(
            comps_df, num_rows="dynamic", width="stretch", hide_index=True, key=f"comps_{aid}",
            column_config={
                "name": st.column_config.TextColumn("Component (what the buyer gets)", width="large", required=True),
                "category": st.column_config.SelectboxColumn("Category", options=["hotel", "meal", "transfer", "tour", "attraction", "activity", "other"]),
                "required": st.column_config.CheckboxColumn("Must match", default=True),
                "match_rule": st.column_config.TextColumn("Match rule (optional)", width="large"),
            },
        )
        if ref.extraction_notes:
            st.caption(md(f"Reader's notes: {ref.extraction_notes}"))
    st.divider()
    if st.button("🔎 Save and search the internet", type="primary"):
        comps = []
        for _, r in edited.iterrows():
            name = r.get("name")
            if isinstance(name, str) and name.strip():
                rule = r.get("match_rule")
                comps.append(Component(name=name.strip(), category=str(r.get("category") or "other"),
                                       required=bool(r.get("required", True)),
                                       match_rule=rule.strip() if isinstance(rule, str) and rule.strip() else None))
        if not comps:
            st.error("Add at least one component.")
            return
        new = ref.model_copy(update=dict(
            title=title, destination=dest, cities=[c.strip() for c in cities.split(",") if c.strip()],
            nights=nights or None, days=days or None, hotel_star=star or None, transfers=transfers or None,
            price_amount=price or None, price_currency=(cur or "USD").upper()[:3], price_basis=basis,
            flights_included=(flights == "Yes"), components=comps))
        if guard(app.start, aid, new) is not _FAILED:
            st.rerun()


# ----------------------------------------------------------------------------- results
def offers_df(offers, ref_usd) -> pd.DataFrame:
    rows = []
    for o in offers:
        if o.verification != "page":
            continue
        ok = bool(o.price_verified)
        native = "no price" if o.price_from is None else f"{o.currency} {o.price_from:,.0f}" + (f" – {o.price_to:,.0f}" if o.price_to else "")
        if o.price_from is not None and not ok:
            native = "⚠ unverified: " + native
        tot = o.components_total or 0
        usd = o.price_usd_from if ok else None
        rows.append({
            "Seller": o.seller, "Type": o.seller_type, "Site": o.listed_on, "Package": o.package_name,
            "Hotel": o.hotel or "", "Stars": o.hotel_star, "Nights": o.nights,
            "Price (as shown)": native, "USD pp": usd,
            "vs yours": signed_money(usd - ref_usd) if (usd is not None and ref_usd) else "",
            "Match": o.match_level, "Components": f"{o.components_matched}/{tot}",
            "Missing / unproven": "; ".join(o.missing),
            "Extras (they offer, you don't)": "; ".join(o.extras), "Price vs content": o.value_note,
            "In stats": "Yes" if o.in_stats else "No",
            "Flights": {True: "Yes", False: "No"}.get(o.flights_included, "?"),
            "Transfers": o.transfers or "?",
            "Match %": (o.components_matched / tot * 100) if tot else 0, "Score": o.match_score,
            "Why rejected": o.reject_reason, "Flags": "; ".join(o.flags), "URL": o.url,
        })
    return pd.DataFrame(rows)


def chart_prices(df: pd.DataFrame, ref_usd) -> go.Figure:
    d = df[df["USD pp"].notna() & (df["Match"] != "rejected")].sort_values("USD pp")
    fig = go.Figure()
    for lv, color in LEVEL_COLORS.items():
        sub = d[d["Match"] == lv]
        if sub.empty:
            continue
        fig.add_bar(x=sub["USD pp"], y=sub["Seller"] + " · " + sub["Site"].str.slice(0, 22), orientation="h", name=lv.capitalize(),
                    marker=dict(color=color, opacity=[1.0 if v == "Yes" else 0.4 for v in sub["In stats"]]),
                    customdata=sub[["Package", "Price (as shown)", "Components"]],
                    hovertemplate="<b>%{y}</b><br>%{customdata[0]}<br>%{x:$,.0f} per person<br>Shown as: %{customdata[1]}"
                                  "<br>Components: %{customdata[2]}<extra></extra>")
    if ref_usd:
        fig.add_vline(x=ref_usd, line_dash="dash", line_color="#D32F2F", annotation_text=f"Your price {money(ref_usd)}", annotation_position="top")
    fig.update_layout(height=max(320, 26 * len(d) + 120), margin=dict(l=10, r=10, t=40, b=10), xaxis_title="USD per person",
                      yaxis=dict(autorange="reversed"), legend_title="Match level", barmode="overlay")
    return fig


def chart_scatter(df: pd.DataFrame, ref_usd) -> go.Figure:
    d = df[df["USD pp"].notna() & (df["Match"] != "rejected")]
    fig = go.Figure()
    for lv, color in LEVEL_COLORS.items():
        sub = d[d["Match"] == lv]
        if not sub.empty:
            fig.add_scatter(x=sub["Match %"], y=sub["USD pp"], mode="markers", name=lv.capitalize(),
                            marker=dict(size=13, color=color, opacity=[1.0 if v == "Yes" else 0.4 for v in sub["In stats"]],
                                        line=dict(width=1, color="white")), text=sub["Seller"],
                            hovertemplate="<b>%{text}</b><br>%{y:$,.0f} · %{x:.0f}% of components<extra></extra>")
    if ref_usd:
        fig.add_hline(y=ref_usd, line_dash="dash", line_color="#D32F2F", annotation_text="Your price")
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10), xaxis_title="Share of your components found on their page (%)",
                      yaxis_title="USD per person")
    return fig


def chart_heatmap(offers, comps: list[str]):
    rows = [o for o in offers if o.verification == "page" and o.match_level in ("same", "near", "partial")][:25]
    if not rows or not comps:
        return None
    code = {"yes": 2, "not_stated": 1, "no": 0}
    z = [[code.get(o.component_matches.get(c, "not_stated"), 1) for c in comps] for o in rows]
    text = [[{"yes": "Yes", "no": "No", "not_stated": "?"}.get(o.component_matches.get(c, "not_stated"), "?") for c in comps] for o in rows]
    fig = go.Figure(go.Heatmap(z=z, x=comps, y=[f"{o.seller} · {o.listed_on[:18]}" for o in rows], text=text, texttemplate="%{text}",
                               colorscale=[[0, "#EF9A9A"], [0.5, "#E0E0E0"], [1, "#A5D6A7"]], zmin=0, zmax=2, showscale=False))
    fig.update_layout(height=max(300, 28 * len(rows) + 140), margin=dict(l=10, r=10, t=10, b=10), xaxis=dict(side="top", tickangle=-30),
                      yaxis=dict(autorange="reversed"))
    return fig


def get_export(app, aid: str, stamp: str):
    key = f"xlsx_{aid}_{stamp}"
    if key not in st.session_state:
        data = guard(app.export_xlsx, aid)
        if data is _FAILED:
            return None
        st.session_state[key] = data
    return st.session_state[key]


def page_results(app, aid: str, d: dict) -> None:
    ref = ReferencePackage(**d["reference"])
    a = d["summary"] or {}
    offers = app.offers(aid)
    ref_usd = a.get("reference_price_usd")

    top = st.columns([4, 1.4, 1.2, 0.8])
    top[0].title(ref.title or ref.destination or "Analysis")
    top[0].caption(md(f"{ref.destination} · {ref.nights or '?'}N/{ref.days or '?'}D · {ref.hotel_star or '?'}★ · run {aid}"))
    data = get_export(app, aid, d["updated_at"])
    if data:
        top[1].download_button("📥 Export to Excel", data=data, file_name=f"competitors_{aid}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               type="primary", width="stretch", on_click="ignore")
    if top[2].button("🔄 Search again", width="stretch"):
        if guard(app.start, aid) is not _FAILED:
            st.rerun()
    if top[3].button("🗑️", help="Delete this analysis"):
        if guard(app.delete, aid) is not _FAILED:
            set_current(None)
            st.rerun()

    if not a.get("in_stats"):
        if a.get("stats_count"):
            st.info(f"No competitor matched closely enough (Same/Near), so the price statistics below use all {a['stats_count']} whole packages "
                    "with a verified price (partial matches). Check the 'Missing / unproven' column in the Competitors tab to see what each one lacks.")
        else:
            st.warning("No competitor with a verified whole-package price was found. Open the 'Pages to check' tab, try 'deep' search depth, "
                       "or add extra search queries.")

    m = st.columns(6)
    m[0].metric("Your price", money(ref_usd), help="USD per person at today's FX")
    m[1].metric("Market median", money(a.get("median")),
                delta=(f"{a['vs_median_pct']:+.1f}% you" if a.get("vs_median_pct") is not None else None), delta_color="off",
                help="Median USD price per person of comparable offers (Same + Near; all priced packages if there is none). The delta shows how far your price is above (+) or below (-) it.")
    m[2].metric("Cheapest", money(a.get("min")))
    m[3].metric("Most expensive", money(a.get("max")))
    m[4].metric("Your rank", f"{a['rank']} of {a['rank_of']}" if a.get("rank") else "n/a", help="1 = cheapest among comparable offers")
    m[5].metric("Offers in statistics", a.get("stats_count", a.get("in_stats", 0)),
                help="Same + Near offers; if there are none, all whole packages with a verified price")
    if a.get("position"):
        st.caption(md(f"Your price is **{a['position']}** (interquartile range {money(a.get('p25'))} – {money(a.get('p75'))})."))
    if a.get("wide_count"):
        st.caption(md(f"All whole packages with a verified price, including partial matches: **{a['wide_count']}** · "
                      f"{money(a.get('wide_min'))} – {money(a.get('wide_max'))} (median {money(a.get('wide_median'))})"))

    vt = a.get("value_verdict_text")
    if vt:
        {"warning": st.warning, "success": st.success}.get(a.get("value_verdict_level"), st.info)(md("**Price vs content:** " + vt))

    t1, t2, tg, t3, t4, t5 = st.tabs(["📊 Analytics", "🏷️ Competitors", "🧩 Gap analysis", "🔗 Pages to check", "📦 Your package", "🪵 Log"])
    df = offers_df(offers, ref_usd)

    with t1:
        shown = df[df["Match"] != "rejected"] if not df.empty else df
        if shown.empty or shown["USD pp"].notna().sum() == 0:
            st.info("No priced offers to chart yet.")
        else:
            st.markdown("##### Price per person by competitor")
            st.caption("Only prices proven on the competitor's page are drawn. Faded bars are not used in the main statistics "
                       "(partial match, flights bundled, etc.) but are shown so you can see what they charge.")
            st.plotly_chart(chart_prices(df, ref_usd), width="stretch")
            c1, c2 = st.columns(2)
            with c1:
                st.markdown("##### Price vs how much of your package they match")
                st.plotly_chart(chart_scatter(df, ref_usd), width="stretch")
            with c2:
                st.markdown("##### Who is selling it")
                by_type = pd.Series(a.get("by_seller_type") or {}).sort_values()
                if not by_type.empty:
                    fig = go.Figure(go.Bar(x=by_type.values, y=by_type.index, orientation="h", marker_color="#1565C0"))
                    fig.update_layout(height=170, margin=dict(l=10, r=10, t=10, b=10))
                    st.plotly_chart(fig, width="stretch")
                lv = a.get("by_level") or {}
                if lv:
                    fig = go.Figure(go.Bar(x=[k.capitalize() for k in lv], y=list(lv.values()), marker_color=[LEVEL_COLORS[k] for k in lv]))
                    fig.update_layout(height=190, margin=dict(l=10, r=10, t=10, b=10), yaxis_title="Offers")
                    st.plotly_chart(fig, width="stretch")
                st.caption(" · ".join(f"**{k}**: {v}" for k, v in LEVEL_HELP.items()))
        comps = [c.name for c in ref.components if c.required]
        hm = chart_heatmap(offers, comps)
        if hm is not None:
            st.markdown("##### Component coverage (green = proven on page, grey = not stated, red = excluded)")
            st.plotly_chart(hm, width="stretch")

    with t2:
        if df.empty:
            st.info("No verified competitor pages.")
        else:
            f1, f2, f3, f4 = st.columns([2, 1.3, 1.3, 2])
            levels = f1.multiselect("Match level", list(LEVEL_COLORS), default=["same", "near", "partial"])
            only_stats = f2.checkbox("Only offers in price stats", value=False)
            hide_flights = f3.checkbox("Hide flight-bundled", value=False)
            q = f4.text_input("Search seller / package / site")
            v = df[df["Match"].isin(levels)]
            if only_stats:
                v = v[v["In stats"] == "Yes"]
            if hide_flights:
                v = v[v["Flights"] != "Yes"]
            if q:
                ql = q.lower()
                v = v[v.apply(lambda r: ql in f"{r['Seller']} {r['Package']} {r['Site']}".lower(), axis=1)]
            v = v.sort_values(["In stats", "Score"], ascending=[False, False])
            cols = ["Seller", "Type", "Site", "Package", "Hotel", "Stars", "Nights", "Price (as shown)", "USD pp", "vs yours", "Match",
                    "Components", "Missing / unproven", "Extras (they offer, you don't)", "Price vs content", "In stats", "Flights", "Transfers", "Match %", "Score"]
            if "rejected" in levels:
                cols.append("Why rejected")
            cols += ["Flags", "URL"]
            st.caption(f"{len(v)} of {len(df)} verified offers shown. Missing items are not a reason to hide a competitor - the price is still shown.")
            st.dataframe(
                v[cols], hide_index=True, width="stretch", height=min(700, 60 + 36 * max(len(v), 3)),
                column_config={
                    "USD pp": st.column_config.NumberColumn(format="$%.0f"),
                    "vs yours": st.column_config.TextColumn(help="Competitor price minus your price"),
                    "Match %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                    "Score": st.column_config.NumberColumn(format="%.0f"),
                    "URL": st.column_config.LinkColumn(display_text="open"),
                },
            )
            with st.expander("Evidence behind each offer (verbatim quotes from the pages)"):
                for o in offers:
                    if o.verification != "page" or o.match_level not in levels:
                        continue
                    st.markdown(md(f"**{o.seller}** · {o.package_name} · [{o.listed_on}]({o.url})"))
                    if o.flags:
                        st.caption(md("⚠️ " + "; ".join(o.flags)))
                    for c in comps:
                        stt = o.component_matches.get(c, "not_stated")
                        mark = {"yes": "✅", "no": "❌"}.get(stt, "❔")
                        q_ = o.evidence.get(c)
                        st.markdown(md(f"{mark} {c}" + (f" — _“{q_}”_" if q_ else "")))
                    st.divider()

    with tg:
        tab_gap(offers, ref, a, ref_usd)

    with t3:
        leads = [o for o in offers if o.verification == "snippet"]
        st.caption("The search found these pages but they could not be read automatically (blocked, JavaScript-only, timeout). Open them by hand.")
        if leads:
            st.dataframe(pd.DataFrame([{"Site": o.listed_on, "Title": o.package_name, "Why": o.flags[0] if o.flags else "", "URL": o.url} for o in leads]),
                         hide_index=True, width="stretch", column_config={"URL": st.column_config.LinkColumn(display_text="open")})
        else:
            st.success("Every candidate page was read successfully.")

    with t4:
        st.json({k: v for k, v in ref.model_dump().items() if k != "components"}, expanded=False)
        st.dataframe(pd.DataFrame([c.model_dump() for c in ref.components]), hide_index=True, width="stretch")

    with t5:
        st.code("\n".join(f"{e['ts'][11:19]} {e['level']:<7} {e['message']}" for e in app.events(aid)), language=None)


def tab_gap(offers, ref, a, ref_usd) -> None:
    pool = [o for o in offers if o.verification == "page" and o.match_level in ("same", "near", "partial")]
    comps = [c.name for c in ref.components if c.required]
    if not pool:
        st.info("No comparable competitor packages yet.")
        return
    st.caption("What you sell vs what each competitor sells. 'Not shown' means their page did not prove the item (they may still include it); "
               "extras are things they include that your package does not, each backed by a quote from their page.")
    cov = a.get("component_coverage") or []
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("##### How many competitors show each of YOUR components")
        if cov:
            fig = go.Figure(go.Bar(x=[c["have"] for c in cov], y=[c["name"] for c in cov], orientation="h", marker_color="#1565C0",
                                   text=[f"{c['have']}/{c['total']}" for c in cov], textposition="auto"))
            fig.update_layout(height=max(260, 26 * len(cov) + 80), margin=dict(l=10, r=10, t=10, b=10), yaxis=dict(autorange="reversed"),
                              xaxis_title="Competitors showing it")
            st.plotly_chart(fig, width="stretch")
            st.caption("Low bars = items few competitors offer (your differentiators, or items competitors rarely list).")
    with c2:
        st.markdown("##### Extras competitors offer that you don't")
        ex = a.get("top_extras") or []
        if ex:
            st.dataframe(pd.DataFrame([{"Extra": e["name"], "Competitors": e["count"], "Median price (USD pp)": e["median_usd"]} for e in ex]),
                         hide_index=True, width="stretch",
                         column_config={"Median price (USD pp)": st.column_config.NumberColumn(format="$%.0f")})
        else:
            st.info("No extras found: competitors do not list anything beyond your components.")

    st.markdown("##### Competitor by competitor")
    rows = []
    for o in sorted(pool, key=lambda x: (x.price_usd_from is None or not x.price_verified, x.price_usd_from or 0)):
        usd = o.price_usd_from if o.price_verified else None
        rows.append({"Seller": o.seller, "Package": o.package_name, "Match": o.match_level, "USD pp": usd,
                     "vs yours": signed_money(usd - ref_usd) if (usd is not None and ref_usd) else "",
                     "They show": f"{o.components_matched}/{o.components_total}", "Your items they don't show": "; ".join(o.missing),
                     "Their extras": "; ".join(o.extras), "Verdict": o.value_note, "URL": o.url})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=min(600, 60 + 36 * max(len(rows), 3)),
                 column_config={"USD pp": st.column_config.NumberColumn(format="$%.0f"), "URL": st.column_config.LinkColumn(display_text="open")})

    st.markdown("##### Side-by-side for one competitor")
    labels = {i: f"{o.seller} · {o.package_name[:45]} · {money(o.price_usd_from) if o.price_verified else 'no price'}" for i, o in enumerate(pool)}
    pick = st.selectbox("Competitor", list(labels), format_func=lambda i: labels[i], key="gap_pick")
    o = pool[pick]
    m1, m2, m3 = st.columns(3)
    m1.metric("Their price (USD pp)", money(o.price_usd_from) if o.price_verified else "n/a")
    m2.metric("Your price", money(ref_usd))
    m3.metric("Difference", signed_money(o.price_usd_from - ref_usd) if (o.price_verified and o.price_usd_from is not None and ref_usd) else "n/a")
    if o.value_note:
        st.info(md(o.value_note))
    both = [c for c in comps if o.component_matches.get(c) == "yes"]
    lack = [c for c in comps if o.component_matches.get(c) != "yes"]
    g1, g2, g3 = st.columns(3)
    with g1:
        st.markdown(f"**✅ You both offer ({len(both)})**")
        for c in both:
            st.markdown(md(f"- {c}" + (f"  \n  _“{o.evidence[c]}”_" if o.evidence.get(c) else "")))
    with g2:
        st.markdown(f"**❌ You offer, they don't show ({len(lack)})**")
        for c in lack:
            st.markdown(md(f"- {c}" + (" _(excluded on their page)_" if o.component_matches.get(c) == "no" else "")))
    with g3:
        st.markdown(f"**➕ They offer, you don't ({len(o.extras)})**")
        for x in o.extras:
            st.markdown(md(f"- {x}" + (f"  \n  _“{o.extra_evidence[x]}”_" if o.extra_evidence.get(x) else "")))
    if o.url:
        st.link_button("Open their page", o.url)


def page_failed(app, aid: str, d: dict) -> None:
    st.error(md(f"This analysis failed: {d.get('error') or d.get('message')}"))
    c1, c2, _ = st.columns([1, 1, 4])
    if c1.button("🔁 Retry", type="primary"):
        if guard(app.retry, aid) is not _FAILED:
            st.rerun()
    if c2.button("🗑️ Delete"):
        if guard(app.delete, aid) is not _FAILED:
            set_current(None)
            st.rerun()
    with st.expander("Log"):
        st.code("\n".join(f"{e['ts'][11:19]} {e['level']:<7} {e['message']}" for e in app.events(aid)), language=None)


# ----------------------------------------------------------------------------- main
def main() -> None:
    if not require_login(get_settings()):
        return
    app = get_app()
    sidebar(app)
    aid = st.session_state.get("aid") or st.query_params.get("id")
    if not aid:
        page_new(app)
        return
    try:
        d = app.get(aid)
    except AppError as e:
        st.error(e.message)
        set_current(None)
        return
    st.session_state["aid"] = aid
    status = d["status"]
    if status in RUNNING:
        page_running(app, aid, d)
    elif status == "awaiting_review" and d["reference"]:
        page_review(app, aid, d)
    elif status == "completed" and d["reference"]:
        page_results(app, aid, d)
    else:
        page_failed(app, aid, d)


main()
