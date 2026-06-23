"""
app.py — Streamlit Real Estate Aggregator Dashboard

Run:
    streamlit run app.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import json
import logging
from datetime import datetime

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.orm import Session

from models import init_db, SessionLocal, Listing, PriceHistory
from utils.engine import (
    get_all_listings_df,
    detect_hot_deals,
    RawListing,
    upsert_listing,
)
import threading

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Lucušine realitky",
    page_icon="🏡",
    layout="wide",
    initial_sidebar_state="expanded",
)

logging.basicConfig(level=logging.WARNING)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Base typography */
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');

    html, body, [class*="css"] {
        font-family: 'IBM Plex Sans', system-ui, sans-serif;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: #0f1117;
        border-right: 1px solid #1e2235;
    }
    section[data-testid="stSidebar"] * { color: #c8d0e7 !important; }
    section[data-testid="stSidebar"] .stSelectbox label,
    section[data-testid="stSidebar"] .stSlider label { color: #8892b0 !important; font-size: 0.78rem; }

    /* Metric cards */
    div[data-testid="metric-container"] {
        background: #131722;
        border: 1px solid #1e2a45;
        border-radius: 8px;
        padding: 1rem 1.2rem;
    }
    div[data-testid="metric-container"] label { color: #8892b0 !important; font-size: 0.78rem; }
    div[data-testid="metric-container"] [data-testid="stMetricValue"] {
        color: #cdd9f5 !important;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 1.6rem;
    }

    /* Hot deal badge */
    .hot-badge {
        display: inline-block;
        background: linear-gradient(135deg, #ff4d4d 0%, #ff8c00 100%);
        color: white;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 12px;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }

    /* Section headers */
    .section-header {
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 0.15em;
        text-transform: uppercase;
        color: #4a90d9;
        margin-bottom: 0.6rem;
        border-bottom: 1px solid #1e2a45;
        padding-bottom: 0.4rem;
    }

    /* Data table tweaks */
    div[data-testid="stDataFrame"] table {
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.82rem;
    }

    /* Price highlight */
    .price-down { color: #27ae60; font-weight: 600; }
    .price-up   { color: #e74c3c; font-weight: 600; }

    /* Note textarea */
    .stTextArea textarea {
        background: #131722 !important;
        color: #c8d0e7 !important;
        border: 1px solid #1e2a45 !important;
        font-family: 'IBM Plex Mono', monospace;
        font-size: 0.82rem;
    }

    /* Streamlit default overrides */
    .main .block-container { padding-top: 1.5rem; }
    h1 { color: #cdd9f5 !important; letter-spacing: -0.02em; }
    h2 { color: #a8b8d8 !important; font-size: 1.1rem !important; }
    h3 { color: #8892b0 !important; font-size: 0.95rem !important; }
</style>
""", unsafe_allow_html=True)


# ── DB init & auto-seed ───────────────────────────────────────────────────────
init_db()

@st.cache_resource
def _auto_seed():
    """Seed demo data on first run (needed on Streamlit Cloud — ephemeral DB)."""
    db = SessionLocal()
    count = db.query(Listing).count()
    db.close()
    if count == 0:
        import seed_demo
        seed_demo.seed(n_normal=120)
    return True

_auto_seed()

@st.cache_resource
def get_session_factory():
    return SessionLocal

def get_db() -> Session:
    return get_session_factory()()


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_price(v: float) -> str:
    return f"{v:,.0f} €".replace(",", "\u202f")

def fmt_sqm(v: float) -> str:
    return f"{v:,.0f} €/m²".replace(",", "\u202f")

def fmt_area(v: float) -> str:
    return f"{v:g} m²"

def save_note(listing_id: int, note: str) -> None:
    db = get_db()
    try:
        listing = db.query(Listing).filter(Listing.id == listing_id).first()
        if listing:
            listing.internal_notes = note
            listing.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()

def get_inactive_count() -> int:
    db = get_db()
    try:
        return db.query(Listing).filter(Listing.status == "inactive").count()
    finally:
        db.close()

def get_price_history(listing_id: int) -> pd.DataFrame:
    db = get_db()
    try:
        rows = (
            db.query(PriceHistory)
            .filter(PriceHistory.listing_id == listing_id)
            .order_by(PriceHistory.changed_at)
            .all()
        )
        return pd.DataFrame([{
            "Dátum": r.changed_at.strftime("%d.%m.%Y %H:%M"),
            "Stará cena": fmt_price(r.old_price) if r.old_price else "—",
            "Nová cena": fmt_price(r.new_price),
            "€/m² pred": fmt_sqm(r.old_price_per_sqm) if r.old_price_per_sqm else "—",
            "€/m² po":  fmt_sqm(r.new_price_per_sqm),
            "Poznámka": r.note or "",
        } for r in rows])
    finally:
        db.close()


# ── Sidebar filters ───────────────────────────────────────────────────────────
def sidebar_filters(df: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.markdown("## 🔍 Filtre")

    cities = ["Všetky"] + sorted(df["location_city"].dropna().unique().tolist())
    sel_city = st.sidebar.selectbox("Mesto / Obec", cities, index=0)

    ptypes_raw = sorted(df["property_type"].dropna().unique().tolist())
    ptype_labels = {"byt": "🏢 Byt", "dom": "🏡 Dom", "pozemok": "🌿 Pozemok", "komercia": "🏪 Komercia", "iny": "❓ Iný"}
    ptype_options = [ptype_labels.get(p, p) for p in ptypes_raw]
    sel_ptype_labels = st.sidebar.multiselect("Typ nehnuteľnosti", ptype_options, default=ptype_options)
    sel_ptypes = [ptypes_raw[ptype_options.index(l)] for l in sel_ptype_labels if l in ptype_options]

    min_price = int(df["absolute_price"].min()) if not df.empty else 0
    max_price = int(df["absolute_price"].max()) if not df.empty else 1_000_000
    price_range = st.sidebar.slider(
        "Cena (€)",
        min_value=min_price, max_value=max_price,
        value=(min_price, max_price), step=5_000,
        format="%d €"
    )

    min_area = int(df["area_sqm"].min()) if not df.empty else 0
    max_area = int(df["area_sqm"].max()) if not df.empty else 500
    area_range = st.sidebar.slider(
        "Plocha (m²)",
        min_value=min_area, max_value=max_area,
        value=(min_area, max_area),
    )

    search_text = st.sidebar.text_input("🔎 Vyhľadať (mesto, typ...)", "")

    # Apply filters
    filtered = df.copy()
    if sel_city != "Všetky":
        filtered = filtered[filtered["location_city"] == sel_city]
    if sel_ptypes:
        filtered = filtered[filtered["property_type"].isin(sel_ptypes)]
    filtered = filtered[
        (filtered["absolute_price"] >= price_range[0]) &
        (filtered["absolute_price"] <= price_range[1])
    ]
    filtered = filtered[
        (filtered["area_sqm"] >= area_range[0]) &
        (filtered["area_sqm"] <= area_range[1])
    ]
    if search_text:
        mask = filtered.apply(
            lambda row: search_text.lower() in str(row).lower(), axis=1
        )
        filtered = filtered[mask]

    return filtered


# ── Views ──────────────────────────────────────────────────────────────────────
def render_kpi_row(df: pd.DataFrame, hot_df: pd.DataFrame) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📋 Inzeráty celkom", len(df))
    c2.metric("🔥 Hot Deals", len(hot_df))
    if not df.empty:
        c3.metric("Medián ceny", fmt_price(df["absolute_price"].median()))
        c4.metric("Medián €/m²", fmt_sqm(df["price_per_sqm"].median()))
        c5.metric("Priem. plocha", fmt_area(df["area_sqm"].mean()))
    else:
        c3.metric("Medián ceny", "—")
        c4.metric("Medián €/m²", "—")
        c5.metric("Priem. plocha", "—")


def render_main_table(df: pd.DataFrame) -> None:
    st.markdown('<p class="section-header">📊 Všetky inzeráty</p>', unsafe_allow_html=True)

    if df.empty:
        st.info("Žiadne inzeráty nezodpovedajú filtrom. Skúste upraviť kritériá alebo spustite seed_demo.py.")
        return

    display_cols = {
        "id": "ID",
        "location_city": "Mesto",
        "location_district": "Štvrť",
        "property_type": "Typ",
        "subtype": "Podtyp",
        "condition": "Stav",
        "area_sqm": "Plocha (m²)",
        "absolute_price": "Cena (€)",
        "price_per_sqm": "€/m²",
        "internal_notes": "Poznámky",
    }

    table_df = df[list(display_cols.keys())].rename(columns=display_cols).copy()
    table_df["Cena (€)"] = table_df["Cena (€)"].apply(lambda x: f"{x:,.0f}")
    table_df["€/m²"] = table_df["€/m²"].apply(lambda x: f"{x:,.0f}")
    table_df["Plocha (m²)"] = table_df["Plocha (m²)"].apply(lambda x: f"{x:g}")

    # Clickable link — first URL from source_urls list
    table_df["🔗 Link"] = df["source_urls"].apply(
        lambda urls: urls[0] if isinstance(urls, list) and urls else None
    )

    st.dataframe(
        table_df,
        use_container_width=True,
        hide_index=True,
        height=420,
        column_config={
            "🔗 Link": st.column_config.LinkColumn(
                "🔗 Link",
                display_text="otvoriť",
            ),
        },
    )

    # ── Note editor ───────────────────────────────────────────────────────────
    st.markdown('<p class="section-header">✏️ Interné poznámky</p>', unsafe_allow_html=True)
    col_sel, col_note = st.columns([1, 2])
    with col_sel:
        ids = df["id"].tolist()
        labels = [
            f"#{row['id']} – {row['location_city']} {row.get('subtype','') or row['property_type']} {fmt_area(row['area_sqm'])}"
            for _, row in df.iterrows()
        ]
        selected_label = st.selectbox("Vybrať inzerát", labels, key="note_sel")
        selected_idx = labels.index(selected_label)
        selected_id = ids[selected_idx]

    with col_note:
        current_note = df[df["id"] == selected_id]["internal_notes"].values[0] or ""
        new_note = st.text_area(
            "Poznámka (uloží sa automaticky po kliknutí)",
            value=current_note,
            height=110,
            key=f"note_{selected_id}",
        )
        if st.button("💾 Uložiť poznámku", key=f"save_{selected_id}"):
            save_note(selected_id, new_note)
            st.success("Poznámka uložená.")
            st.rerun()

    # Price history expander
    with st.expander(f"📈 Historia cien – inzerát #{selected_id}"):
        hist_df = get_price_history(selected_id)
        if hist_df.empty:
            st.caption("Žiadna história cien.")
        else:
            st.dataframe(hist_df, use_container_width=True, hide_index=True)


def render_hot_deals(hot_df: pd.DataFrame, all_df: pd.DataFrame) -> None:
    st.markdown('<p class="section-header">🔥 Hot Deals — matematicky overené anomálie</p>', unsafe_allow_html=True)

    if hot_df.empty:
        st.info(
            "Žiadne Hot Deals v aktuálnom výbere. "
            "Hot Deal = cena/m² ≤ 80 % mediánu segmentu (po odstránení outlierov metódou IQR)."
        )
        return

    # Summary chart
    fig = go.Figure()
    for _, row in hot_df.iterrows():
        label = f"#{int(row['id'])} {row['location_city']}"
        fig.add_bar(
            x=[label],
            y=[row["clean_median_sqm"]],
            name="Medián segmentu",
            marker_color="#3a4a6b",
            showlegend=(_ == hot_df.index[0]),
        )
        fig.add_bar(
            x=[label],
            y=[row["price_per_sqm"]],
            name="Cena inzerátu",
            marker_color="#e74c3c",
            showlegend=(_ == hot_df.index[0]),
        )

    fig.update_layout(
        barmode="group",
        title="Porovnanie: cena inzerátu vs. medián segmentu (€/m²)",
        yaxis_title="€/m²",
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
        font_color="#8892b0",
        legend=dict(bgcolor="#131722", bordercolor="#1e2a45"),
        title_font_color="#cdd9f5",
        height=320,
        margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    # Individual hot deal cards
    for _, row in hot_df.iterrows():
        with st.container():
            cols = st.columns([3, 2, 2, 2, 1])
            cols[0].markdown(
                f"**#{int(row['id'])} — {row['location_city']}** "
                f"{'(' + row['location_district'] + ')' if pd.notna(row.get('location_district')) and row.get('location_district') else ''}<br>"
                f"<small>{row.get('subtype','') or row['property_type']} • {fmt_area(row['area_sqm'])} • {row.get('condition','') or '—'}</small>",
                unsafe_allow_html=True
            )
            cols[1].metric("Cena", fmt_price(row["absolute_price"]))
            cols[2].metric(
                "€/m² (inzerát)",
                fmt_sqm(row["price_per_sqm"]),
                delta=f"−{row['discount_pct']:.1f}% od mediánu",
                delta_color="inverse",
            )
            cols[3].metric("Medián segmentu", fmt_sqm(row["clean_median_sqm"]))
            cols[4].markdown(
                f'<span class="hot-badge">−{row["discount_pct"]:.0f}%</span>',
                unsafe_allow_html=True
            )

            # Source URLs
            urls = row.get("source_urls", [])
            if isinstance(urls, str):
                try:
                    urls = json.loads(urls)
                except Exception:
                    urls = []
            if urls:
                url_links = " · ".join([f"[zdroj {i+1}]({u})" for i, u in enumerate(urls)])
                st.caption(f"🔗 {url_links}")

            # Note for hot deal
            note_key = f"hot_note_{int(row['id'])}"
            current_note = str(row.get("internal_notes", "") or "")
            with st.expander("✏️ Poznámka k tomuto inzerátu"):
                new_note = st.text_area("", value=current_note, height=80, key=note_key)
                if st.button("Uložiť", key=f"hot_save_{int(row['id'])}"):
                    save_note(int(row["id"]), new_note)
                    st.success("Uložené.")
                    st.rerun()

            st.markdown(
                f"<small style='color:#4a5568'>Segment: <code>{row['segment_key']}</code> "
                f"| n={row['segment_n_total']} (po IQR: {row['segment_n_clean']}) "
                f"| IQR threshold: ≤ {fmt_sqm(row['hot_deal_threshold_sqm'])}</small>",
                unsafe_allow_html=True,
            )
            st.divider()


def render_analytics(df: pd.DataFrame) -> None:
    st.markdown('<p class="section-header">📈 Analytika trhu</p>', unsafe_allow_html=True)
    if df.empty:
        st.info("Nie sú k dispozícii dáta pre analýzu.")
        return

    col1, col2 = st.columns(2)

    with col1:
        fig_box = px.box(
            df, x="property_type", y="price_per_sqm",
            color="property_type",
            title="Distribúcia €/m² podľa typu",
            labels={"property_type": "Typ", "price_per_sqm": "€/m²"},
            color_discrete_sequence=px.colors.qualitative.Set2,
        )
        fig_box.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="#8892b0", title_font_color="#cdd9f5",
            showlegend=False, height=320,
            margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(fig_box, use_container_width=True)

    with col2:
        city_counts = df["location_city"].value_counts().reset_index()
        city_counts.columns = ["Mesto", "Počet"]
        fig_bar = px.bar(
            city_counts, x="Mesto", y="Počet",
            title="Počet inzerátov podľa mesta",
            color="Počet",
            color_continuous_scale="Blues",
        )
        fig_bar.update_layout(
            plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
            font_color="#8892b0", title_font_color="#cdd9f5",
            height=320, showlegend=False,
            margin=dict(l=10, r=10, t=40, b=10),
            coloraxis_showscale=False,
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    # Scatter: area vs price
    fig_scatter = px.scatter(
        df, x="area_sqm", y="absolute_price",
        color="property_type",
        hover_data=["location_city", "price_per_sqm", "subtype"],
        title="Plocha vs. Absolútna cena",
        labels={"area_sqm": "Plocha (m²)", "absolute_price": "Cena (€)", "property_type": "Typ"},
        color_discrete_sequence=px.colors.qualitative.Set2,
        opacity=0.75,
    )
    fig_scatter.update_layout(
        plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
        font_color="#8892b0", title_font_color="#cdd9f5",
        height=380, margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig_scatter, use_container_width=True)


def render_add_listing_form() -> None:
    """Manual data entry form for testing without scraper."""
    with st.expander("➕ Ručne pridať inzerát (testovanie)"):
        with st.form("add_listing"):
            c1, c2, c3 = st.columns(3)
            city = c1.text_input("Mesto *", placeholder="napr. Nitra")
            district = c2.text_input("Štvrť", placeholder="napr. Chrenová")
            ptype = c3.selectbox("Typ *", ["byt", "dom", "pozemok", "komercia", "iny"])

            c4, c5, c6 = st.columns(3)
            area = c4.number_input("Plocha (m²) *", min_value=1.0, value=70.0, step=1.0)
            price = c5.number_input("Cena (€) *", min_value=1000.0, value=150000.0, step=1000.0)
            url = c6.text_input("URL inzerátu", placeholder="https://...")

            subtype = st.text_input("Podtyp", placeholder="napr. 3-izbový byt")
            condition = st.selectbox("Stav", ["", "pôvodný stav", "čiastočná rekonštrukcia", "po rekonštrukcii", "novostavba"])

            submitted = st.form_submit_button("Pridať inzerát")
            if submitted:
                if not city or area <= 0 or price <= 0:
                    st.error("Vyplňte povinné polia: Mesto, Plocha, Cena.")
                else:
                    raw = RawListing(
                        source_url=url or f"manual-{datetime.utcnow().timestamp()}",
                        location_city=city.strip(),
                        location_district=district.strip() or None,
                        property_type=ptype,
                        area_sqm=float(area),
                        absolute_price=float(price),
                        subtype=subtype.strip() or None,
                        condition=condition or None,
                    )
                    db = get_db()
                    try:
                        _, created = upsert_listing(db, raw)
                        if created:
                            st.success(f"Inzerát pridaný! Mesto: {city}, Cena: {fmt_price(price)}")
                        else:
                            st.info("Inzerát bol identifikovaný ako duplikát a zlúčený s existujúcim záznamom.")
                        st.rerun()
                    finally:
                        db.close()


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Header
    st.markdown(
        """
        <h1 style="margin-bottom:0">🏡 Lucušine realitky</h1>
        <p style="color:#4a5568;font-size:0.85rem;margin-top:4px">
            Nitriansky okres • automatická detekcia anomálií • matematicky overené Hot Deals
        </p>
        """,
        unsafe_allow_html=True,
    )

    # Load data
    db = get_db()
    try:
        all_df = get_all_listings_df(db)
        hot_df = detect_hot_deals(db)
    finally:
        db.close()

    if all_df.empty:
        st.warning(
            "⚠️  Databáza je prázdna. Spustite najprv: `python seed_demo.py` alebo pridajte inzerát ručne nižšie."
        )
        render_add_listing_form()
        return

    # Apply sidebar filters
    filtered_df = sidebar_filters(all_df)

    # Re-detect hot deals from filtered set if filters are active
    # (hot deals computed on full DB for math accuracy, but display filtered)
    if not hot_df.empty and not filtered_df.empty:
        hot_filtered = hot_df[hot_df["id"].isin(filtered_df["id"].tolist())]
    else:
        hot_filtered = hot_df

    # KPI row
    render_kpi_row(filtered_df, hot_filtered)
    st.divider()

    # Tab navigation
    tab1, tab2, tab3 = st.tabs(["📋 Všetky inzeráty", "🔥 Hot Deals", "📈 Analytika"])

    with tab1:
        render_main_table(filtered_df)
        render_add_listing_form()

    with tab2:
        render_hot_deals(hot_filtered, all_df)

    with tab3:
        render_analytics(filtered_df)

    # Footer
    st.sidebar.divider()
    inactive_count = get_inactive_count()
    st.sidebar.caption(
        f"Aktívne: **{len(all_df)}** | Neaktívne: **{inactive_count}**\n\n"
        "Formula Hot Deal:\n"
        "`P_sqm ≤ 0.80 × medián_segmentu`\n"
        "(po IQR očistení outlierov)"
    )
    if st.sidebar.button("🔄 Obnoviť dáta"):
        st.cache_resource.clear()
        st.rerun()

    st.sidebar.divider()
    st.sidebar.markdown("## ⚙️ Scrapery")

    scraper_choice = st.sidebar.selectbox(
        "Portál",
        ["Všetky", "nehnutelnosti", "bazos", "topreality"],
        key="scraper_choice",
    )

    if st.sidebar.button("▶ Spustiť scraping", key="run_scrapers_btn"):
        filter_val = None if scraper_choice == "Všetky" else scraper_choice
        with st.sidebar:
            with st.spinner(f"Scraping {scraper_choice}... (môže trvať niekoľko minút)"):
                try:
                    import run_scrapers
                    result = run_scrapers.run(
                        scraper_filter=filter_val,
                        dry_run=False,
                        mark_inactive=True,
                    )
                    st.success(
                        f"✅ Hotovo!\n\n"
                        f"Nové: **{result['inserted']}** | "
                        f"Aktualizované: **{result['merged']}** | "
                        f"Neaktívne: **{result['inactive']}** | "
                        f"Chyby: **{result['errors']}**"
                    )
                    st.cache_resource.clear()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Chyba pri scrapovaní: {exc}")


if __name__ == "__main__":
    main()
