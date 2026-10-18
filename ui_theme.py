import streamlit as st

_SHARED = """
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap');
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
}
html, body, [class*="css"] { font-family: 'DM Sans', 'Aptos', sans-serif; color: var(--ink); }
[data-testid="stAppViewContainer"] { color: var(--ink); background: #f5f8fb; }
[data-testid="stHeader"] { background: transparent; }
[data-baseweb="input"] > div, [data-baseweb="textarea"] > div,
[data-baseweb="select"] > div, [data-testid="stDateInput"] input,
[data-testid="stTextInput"] input, [data-testid="stTextArea"] textarea {
    color: var(--ink) !important; background: rgba(255,255,255,.94) !important;
    border-color: #cad9e6 !important; border-radius: 9px !important;
}
[data-baseweb="select"] [role="option"], [data-baseweb="popover"] [role="listbox"] {
    color: var(--ink) !important; background: #fff !important;
}
[data-testid="stRadio"] label, [data-testid="stCheckbox"] label,
[data-testid="stSelectbox"] label, [data-testid="stTextInput"] label { color: var(--ink) !important; }
[data-testid="stMetric"] {
    background: #fff; border: 1px solid var(--line); border-radius: 12px;
    padding: .9rem 1.1rem; box-shadow: 0 1px 2px rgba(23,52,81,.04);
}
[data-testid="stMetricLabel"] { color: var(--muted); font-size: .86rem; }
[data-testid="stMetricValue"] { color: var(--ink); font-weight: 700; animation: metric-arrive .32s ease-out both; }
[data-testid="stVerticalBlockBorderWrapper"] {
    background: rgba(255,255,255,.86); border-color: rgba(205,220,233,.9); border-radius: 14px;
    box-shadow: 0 8px 28px rgba(35,75,110,.055); backdrop-filter: blur(8px);
}
[data-testid="stDataFrame"] { color: var(--ink); background: #fff; border: 1px solid var(--line); border-radius: 10px; }
[data-testid="stExpander"] { background: rgba(255,255,255,.8); border: 1px solid var(--line); border-radius: 10px; }
[data-testid="stAlert"] { background: rgba(255,255,255,.9); color: var(--ink); border-radius: 10px; }
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li { color: var(--ink); line-height: 1.6; }
h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
h1 { font-size: 2rem; font-weight: 800; }
h2 { font-size: 1.3rem; font-weight: 700; }
h3 { font-size: 1.05rem; font-weight: 700; }
[data-testid="stBaseButton-primary"] {
    color: #fff; background: var(--blue); border-color: var(--blue); border-radius: 10px;
    font-weight: 650; padding: .55rem 1.3rem;
}
[data-testid="stBaseButton-primary"]:hover { background: #1f629c; border-color: #1f629c; }
[data-testid="stBaseButton-secondary"] { color: var(--ink); background: rgba(255,255,255,.8); border-color: var(--line); border-radius: 10px; }
[data-testid="stBaseButton-secondary"]:hover { color: var(--blue); border-color: #9fc3e5; background: white; }
.eyebrow { color: #28638f; font-size: .76rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; }
.page-subtitle { color: var(--muted); font-size: 1.05rem; margin-bottom: .8rem; max-width: 760px; }
.evidence-strong { color: #1d6b4b; background: #e6f4ec; padding: .3rem .7rem; border-radius: 999px; font-weight: 700; font-size: .85rem; display: inline-block; }
.evidence-medium { color: #775318; background: #fff2d6; padding: .3rem .7rem; border-radius: 999px; font-weight: 700; font-size: .85rem; display: inline-block; }
.evidence-early, .evidence-none { color: #496176; background: #e8f1f7; padding: .3rem .7rem; border-radius: 999px; font-weight: 700; font-size: .85rem; display: inline-block; }
.empty-calm { color: #31566e; font-size: 1.2rem; font-weight: 650; margin: .2rem 0; }
@keyframes page-arrive { from { opacity: .85; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
@keyframes metric-arrive { from { opacity: .65; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }
[data-testid="stMainBlockContainer"] { animation: page-arrive .28s ease-out both; }
.mono { font-family: 'IBM Plex Mono', monospace; }
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; scroll-behavior: auto !important; }
}
"""

_PUBLIC = """
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] { visibility: hidden; height: 0; }
[data-testid="stSidebarNav"] { display: none; }
[data-testid="stSidebar"] { display: none; }
[data-testid="stAppViewContainer"] > .main { padding-left: 0; }
[data-testid="stMainBlockContainer"] { max-width: 880px; padding: 2.4rem 1.5rem 4rem; margin: 0 auto; }
.brand-row { display: flex; align-items: center; gap: .6rem; margin-bottom: 1.6rem; }
.brand-mark { width: 34px; height: 34px; border-radius: 9px; background: linear-gradient(135deg, var(--navy), var(--blue));
              display: inline-flex; align-items: center; justify-content: center; color: #fff; font-weight: 800; font-size: 1.1rem; }
.brand-name { font-weight: 800; font-size: 1.15rem; color: var(--navy); letter-spacing: .01em; }
.hero-title { font-size: 2.4rem; font-weight: 800; line-height: 1.15; color: var(--ink); margin: 0 0 .5rem; }
.hero-sub { color: var(--muted); font-size: 1.12rem; line-height: 1.55; margin: 0 0 2rem; max-width: 620px; }
[data-testid="stFileUploaderDropzone"] {
    background: linear-gradient(180deg, #ffffff, #f7fbff);
    border: 2px dashed #a9c7e3 !important; border-radius: 18px !important;
    padding: 1.6rem !important; transition: border-color .15s, background .15s;
}
[data-testid="stFileUploaderDropzone"]:hover { border-color: var(--blue) !important; background: #f2f8fd; }
[data-testid="stFileUploaderDropzoneInstructions"] svg { color: var(--blue); }
.step-row { display: flex; gap: 1.6rem; margin: 2.2rem 0 1.6rem; flex-wrap: wrap; }
.step { flex: 1; min-width: 160px; }
.step-num { width: 26px; height: 26px; border-radius: 50%; background: var(--blue-pale); color: var(--blue);
            display: inline-flex; align-items: center; justify-content: center; font-weight: 800; font-size: .85rem; margin-bottom: .5rem; }
.step-label { font-weight: 700; color: var(--ink); font-size: .95rem; }
.step-copy { color: var(--muted); font-size: .86rem; margin-top: .15rem; }
/* Result cards are real st.container(border=True, key="result_...") blocks —
   this targets them specifically (vs. other bordered containers on the page)
   without needing one CSS rule per dynamic key. */
[class*="st-key-result_"] [data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 16px !important; box-shadow: 0 4px 18px rgba(23,52,81,.06) !important;
}
.result-title { font-weight: 700; font-size: 1.08rem; color: var(--ink); margin-bottom: .3rem; }
.result-end { font-size: 1.55rem; font-weight: 800; color: var(--navy); }
.result-end-label { color: var(--muted); font-size: .82rem; font-weight: 650; text-transform: uppercase; letter-spacing: .04em; }
.result-meta { color: var(--muted); font-size: .9rem; margin-top: .15rem; }
.trust-note { color: var(--muted); font-size: .82rem; text-align: center; margin-top: 2.4rem; }
@media (max-width: 700px) {
    [data-testid="stMainBlockContainer"] { padding: 1.3rem 1rem 3rem; }
    .hero-title { font-size: 1.7rem; }
    .step-row { flex-direction: column; gap: 1rem; }
}
"""

_INTERNAL = """
[data-testid="stMainBlockContainer"] { max-width: 1440px; padding: 1.6rem 2.2rem 3rem; }
[data-testid="stSidebar"] { background: #ffffff; border-right: 1px solid var(--line); }
[data-testid="stSidebar"]::before {
    content: ""; display: block; height: 4px; background: linear-gradient(90deg, var(--navy), var(--blue));
}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
[data-testid="stSidebar"] label { color: var(--muted); }
.radar-brand { color: var(--navy); font-size: 1.12rem; font-weight: 700; letter-spacing: .04em; padding: .5rem 0 .1rem; }
.radar-brand-sub { color: var(--muted); font-size: .8rem; margin-bottom: .7rem; }
@media (max-width: 700px) { [data-testid="stMainBlockContainer"] { padding: 1rem .85rem 2rem; } }
"""


def apply_theme(mode: str = "internal"):
    """mode="public": the plain, single-purpose upload page — Streamlit's own
    chrome (sidebar, menu, footer) is hidden so it reads as our own product.
    mode="internal": today's fuller admin styling, with the sidebar and its
    native page switcher left visible."""
    css = _SHARED + (_PUBLIC if mode == "public" else _INTERNAL)
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)
