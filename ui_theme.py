from datetime import datetime

import streamlit as st


def environment_for_hour(hour: int) -> tuple[str, str, str]:
    if 5 <= hour < 11:
        return "morning", "234, 244, 252", "255, 240, 210"
    if 11 <= hour < 17:
        return "afternoon", "220, 240, 252", "255, 255, 255"
    if 17 <= hour < 21:
        return "evening", "249, 233, 226", "255, 226, 202"
    return "night", "226, 235, 248", "244, 247, 255"


def apply_theme(now: datetime | None = None):
    period, sky_rgb, light_rgb = environment_for_hour((now or datetime.now().astimezone()).hour)
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
        :root {
            --ink: #18304a;
            --muted: #536b82;
            --paper: #f7faff;
            --white: rgba(255,255,255,.9);
            --navy: #173451;
            --navy-soft: #e5f0f8;
            --blue: #2876b8;
            --blue-pale: #e8f3fb;
            --green: #247553;
            --green-pale: #e8f5ee;
            --amber: #8b5a16;
            --amber-pale: #fff4df;
            --red: #a93636;
            --line: #d7e3ed;
            --sky-wash: __SKY_WASH__;
            --light-wash: __LIGHT_WASH__;
        }
        html, body, [class*="css"] { font-family: 'DM Sans', 'Aptos', sans-serif; color: var(--ink); }
        [data-testid="stAppViewContainer"] {
            color: var(--ink);
            background:
                radial-gradient(ellipse at 82% -8%, rgba(var(--light-wash), .82) 0, transparent 35%),
                radial-gradient(ellipse at 8% 0%, rgba(var(--sky-wash), .72) 0, transparent 42%),
                linear-gradient(165deg, #f8fbfe 0%, #f5f9fd 52%, #f3f8fb 100%);
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stMainBlockContainer"] { max-width: 1380px; padding: 1.6rem 2rem 3rem; }
        [data-testid="stSidebar"] { background: rgba(250,253,255,.9); border-right: 1px solid var(--line); }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] label { color: var(--muted); }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h1,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h2 { color: var(--ink); }
        [data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] {
            color: #354d64; background: transparent; border-color: transparent;
            justify-content: flex-start; text-align: left;
        }
        [data-testid="stSidebar"] [data-testid="stBaseButton-secondary"]:hover {
            color: #174d79; background: #e8f3fb; border-color: #d7e8f4;
        }
        [data-testid="stSidebar"] [data-testid="stBaseButton-primary"] {
            color: #174d79; background: #e4f1fa; border-color: #cde2f0;
            justify-content: flex-start; text-align: left;
        }
        [data-testid="stBaseButton-primary"] {
            color: #fff; background: var(--blue); border-color: var(--blue); border-radius: 7px;
        }
        [data-testid="stBaseButton-secondary"] { color: var(--ink); background: rgba(255,255,255,.7); border-color: var(--line); border-radius: 7px; }
        [data-testid="stBaseButton-secondary"]:hover { color: var(--blue); border-color: #9fc3e5; background: white; }
        [data-baseweb="input"] > div, [data-baseweb="textarea"] > div,
        [data-baseweb="select"] > div, [data-testid="stDateInput"] input,
        [data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {
            color: var(--ink) !important; background: rgba(255,255,255,.94) !important;
            border-color: #cad9e6 !important; border-radius: 7px !important;
        }
        [data-baseweb="select"] [role="option"], [data-baseweb="popover"] [role="listbox"] {
            color: var(--ink) !important; background: #fff !important;
        }
        [data-testid="stRadio"] label, [data-testid="stCheckbox"] label,
        [data-testid="stSelectbox"] label, [data-testid="stTextInput"] label { color: var(--ink) !important; }
        [data-testid="stMetric"] {
            background: transparent; border: 0; border-right: 1px solid var(--line); border-radius: 0;
            padding: .65rem 1.1rem; box-shadow: none;
        }
        [data-testid="column"]:last-child [data-testid="stMetric"] { border-right: 0; }
        [data-testid="stMetricLabel"] { color: var(--muted); font-size: .86rem; }
        [data-testid="stMetricValue"] { color: var(--ink); font-weight: 650; animation: metric-arrive .32s ease-out both; }
        [data-testid="stVerticalBlockBorderWrapper"] {
            background: rgba(255,255,255,.76); border-color: rgba(205,220,233,.9); border-radius: 11px;
            box-shadow: 0 8px 28px rgba(35,75,110,.055); backdrop-filter: blur(8px);
        }
        [data-testid="stDataFrame"] { color: var(--ink); background: #fff; border: 1px solid var(--line); border-radius: 8px; }
        [data-testid="stExpander"] { background: rgba(255,255,255,.72); border: 1px solid var(--line); border-radius: 8px; }
        [data-testid="stAlert"] { background: rgba(255,255,255,.86); color: var(--ink); border-radius: 8px; }
        [data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li { color: var(--ink); line-height: 1.55; }
        h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
        h1 { font-size: 2rem; font-weight: 700; }
        h2 { font-size: 1.3rem; font-weight: 650; }
        h3 { font-size: 1.05rem; font-weight: 650; }
        .radar-brand { color: var(--ink); font-size: 1.12rem; font-weight: 700; padding: .5rem 0 .1rem; }
        .radar-brand-sub { color: var(--muted); font-size: .8rem; margin-bottom: .7rem; }
        .nav-section { color: #55718a; font-size: .76rem; font-weight: 650; margin: 1rem 0 .3rem; }
        .eyebrow { color: #28638f; font-size: .76rem; font-weight: 650; text-transform: uppercase; }
        .page-title { color: var(--ink); font-size: 2rem; font-weight: 700; line-height: 1.18; margin: .3rem 0 .35rem; }
        .page-subtitle { color: var(--muted); font-size: 1rem; margin-bottom: .8rem; }
        .welcome-title { color: var(--ink); font-size: 1.45rem; font-weight: 650; margin: .2rem 0; }
        .welcome-copy { color: var(--muted); font-size: 1rem; margin: 0 0 1rem; }
        .status-pill { display: inline-flex; align-items: center; gap: .5rem; padding: .45rem .7rem; border-radius: 999px; color: #245a43; background: var(--green-pale); font-size: .88rem; font-weight: 650; }
        .status-dot { width: 9px; height: 9px; background: var(--green); border-radius: 50%; }
        .status-pill.paused { color: #744c17; background: var(--amber-pale); }
        .status-pill.paused .status-dot { background: var(--amber); }
        .status-pill.scanning .status-dot { animation: radar-pulse 1.6s ease-out infinite; }
        @keyframes radar-pulse { 0% { box-shadow: 0 0 0 0 rgba(36,117,83,.36); } 70% { box-shadow: 0 0 0 7px rgba(36,117,83,0); } 100% { box-shadow: 0 0 0 0 rgba(36,117,83,0); } }
        .opportunity-window { color: #815511; font-size: .95rem; font-weight: 650; }
        .evidence-strong { color: #1d6b4b; background: #e6f4ec; padding: .28rem .55rem; border-radius: 999px; font-weight: 650; }
        .evidence-medium { color: #775318; background: #fff2d6; padding: .28rem .55rem; border-radius: 999px; font-weight: 650; }
        .evidence-early { color: #496176; background: #e8f1f7; padding: .28rem .55rem; border-radius: 999px; font-weight: 650; }
        .empty-calm { color: #31566e; font-size: 1.25rem; font-weight: 650; margin: .2rem 0; }
        .timeline-date { color: #275f8a; font-size: .94rem; font-weight: 650; }
        .live-note { color: var(--muted); font-size: .92rem; }
        @keyframes page-arrive { from { opacity: .82; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
        @keyframes metric-arrive { from { opacity: .65; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
        [data-testid="stMainBlockContainer"] { animation: page-arrive .24s ease-out both; }
        .mono { font-family: 'IBM Plex Mono', monospace; }
        @media (max-width: 700px) {
            [data-testid="stMainBlockContainer"] { padding: 1rem .85rem 2rem; }
            .page-title { font-size: 1.65rem; }
            .welcome-title { font-size: 1.3rem; }
            [data-testid="stMetric"] { padding: .55rem .45rem; border-right: 0; }
            [data-testid="stMetricLabel"] { font-size: .8rem; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; }
        }
        </style>
        """.replace("__SKY_WASH__", sky_rgb).replace("__LIGHT_WASH__", light_rgb),
        unsafe_allow_html=True,
    )