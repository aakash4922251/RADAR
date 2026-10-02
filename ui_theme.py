import streamlit as st


def apply_theme():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
        :root {
            --ink: #152238;
            --muted: #66758a;
            --paper: #f4f7fb;
            --white: #ffffff;
            --navy: #14243a;
            --navy-soft: #203651;
            --blue: #2876c7;
            --blue-pale: #e8f2fc;
            --green: #22815d;
            --green-pale: #e8f5ef;
            --amber: #a66713;
            --amber-pale: #fff4df;
            --red: #b44444;
            --line: #e1e7ef;
        }
        html, body, [class*="css"] { font-family: 'DM Sans', 'Aptos', sans-serif; }
        [data-testid="stAppViewContainer"] {
            color: var(--ink);
            background: radial-gradient(ellipse at 75% 0%, #e8f0f8 0, transparent 42%), var(--paper);
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stMainBlockContainer"] { max-width: 1440px; padding-top: 2rem; }
        [data-testid="stSidebar"] { background: var(--navy); border-right: 1px solid #263b55; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] label { color: #cfdae8; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2 { color: #fff; }
        [data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
            color: #d9e3ef; background: transparent; border-color: transparent;
            justify-content: flex-start; text-align: left;
        }
        [data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover {
            color: #fff; background: #223852; border-color: #314b68;
        }
        [data-testid="stSidebar"] [data-testid="stBaseButton-primary"] {
            color: #fff; background: #2f669d; border-color: #4984ba;
            justify-content: flex-start; text-align: left;
        }
        [data-testid="stBaseButton-primary"] {
            color: #fff; background: var(--blue); border-color: var(--blue);
        }
        [data-testid="stBaseButton-secondary"] { color: var(--ink); border-color: var(--line); }
        [data-testid="stBaseButton-secondary"]:hover { color: var(--blue); border-color: #9fc3e5; }
        [data-testid="stMetric"] {
            background: var(--white); border: 1px solid var(--line); border-radius: 7px;
            padding: 1rem 1.1rem; box-shadow: 0 3px 12px rgba(21, 34, 56, .035);
        }
        [data-testid="stMetricLabel"] { color: var(--muted); }
        [data-testid="stMetricValue"] { color: var(--ink); font-weight: 700; }
        [data-testid="stVerticalBlockBorderWrapper"] {
            background: var(--white); border-color: var(--line); border-radius: 7px;
            box-shadow: 0 4px 16px rgba(21, 34, 56, .035);
        }
        [data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 7px; }
        h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
        h1 { font-size: 2rem; font-weight: 700; }
        h2 { font-size: 1.35rem; font-weight: 650; }
        h3 { font-size: 1.05rem; font-weight: 650; }
        .radar-brand { color: white; font-size: 1.25rem; font-weight: 700; padding: .5rem 0 .1rem; }
        .radar-brand-sub { color: #94a8bf; font-size: .73rem; margin-bottom: 1rem; }
        .nav-section { color: #8298b1; font-size: .68rem; font-weight: 700; margin: 1.1rem 0 .25rem; }
        .eyebrow { color: var(--blue); font-family: 'IBM Plex Mono', monospace; font-size: .72rem; font-weight: 500; text-transform: uppercase; }
        .page-title { color: var(--ink); font-size: 1.95rem; font-weight: 700; line-height: 1.15; margin: .35rem 0 .4rem; }
        .page-subtitle { color: var(--muted); font-size: .94rem; margin-bottom: 1.1rem; }
        .status-pill { display: inline-flex; align-items: center; gap: .45rem; padding: .38rem .65rem; border-radius: 4px; color: #37526e; background: #eaf0f6; font-size: .75rem; font-weight: 650; }
        .status-dot { width: 7px; height: 7px; background: var(--green); border-radius: 50%; }
        .status-pill.paused { color: #85571c; background: var(--amber-pale); }
        .status-pill.paused .status-dot { background: var(--amber); }
        .opportunity-window { color: var(--amber); font-size: .8rem; font-weight: 700; }
        .mono { font-family: 'IBM Plex Mono', monospace; }
        @media (max-width: 700px) {
            [data-testid="stMainBlockContainer"] { padding: 1rem .8rem 2rem; }
            .page-title { font-size: 1.55rem; }
            [data-testid="stMetric"] { padding: .7rem; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )