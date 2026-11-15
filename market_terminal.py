"""
Market Terminal
  Markets  : US indices, ETFs, stocks and any optionable symbol (Yahoo Finance), plus BTC/ETH (Deribit)
  Analytics: GEX / DEX / VEX / charm, expiry breakdown, profiles, strike ladder, key levels,
             squeeze screener, IV / GEX / charm surfaces, replay + paper trading
  Macro    : economic calendar (CPI, FOMC, NFP...) with countdowns, market and macro headlines
  Speed    : cached data + calculations, one view rendered at a time, threaded downloads, no polling by default

Install:  py -m pip install streamlit yfinance numpy pandas plotly requests
Run:      py -m streamlit run market_terminal.py
Optional: py -m pip install MetaTrader5   (Windows, live CFD price)
"""
import html as _html
import json
import math
import re
import time
import warnings
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import yfinance as yf
from plotly.subplots import make_subplots

warnings.simplefilter("ignore")
st.set_page_config(page_title="Market Terminal", page_icon="📈", layout="wide", initial_sidebar_state="expanded")
S = st.session_state
esc = _html.escape
UA = {"User-Agent": "Mozilla/5.0"}
PLOT_CFG = {"displayModeBar": False, "scrollZoom": False}


# ============================================================ markets & constants
def M(name, group, div=0.5, cfd=None, candle=None, news=None):
    return dict(name=name, group=group, div=div, cfd=cfd, candle=candle, news=news or name)


MARKETS = {
    "^SPX": M("S&P 500 Index", "Indices", 1.3, "US500", news="S&P 500"),
    "^NDX": M("Nasdaq-100 Index", "Indices", 0.7, "US100", news="Nasdaq 100"),
    "^RUT": M("Russell 2000 Index", "Indices", 1.2, "US2000", news="Russell 2000"),
    "SPY": M("SPDR S&P 500 ETF", "ETFs", 1.2, "US500", news="SPY S&P 500 ETF"),
    "QQQ": M("Invesco QQQ (Nasdaq-100)", "ETFs", 0.6, "US100", news="QQQ Nasdaq 100 ETF"),
    "IWM": M("iShares Russell 2000", "ETFs", 1.1, "US2000", news="IWM Russell 2000 ETF"),
    "DIA": M("SPDR Dow Jones", "ETFs", 1.5, "US30", news="Dow Jones DIA ETF"),
    "GLD": M("SPDR Gold Shares", "ETFs", 0.0, news="gold price GLD"),
    "SLV": M("iShares Silver Trust", "ETFs", 0.0, news="silver price SLV"),
    "TLT": M("20+ Year Treasury ETF", "ETFs", 3.5, news="Treasury yields TLT"),
    "XLF": M("Financials Select Sector", "ETFs", 1.4),
    "XLE": M("Energy Select Sector", "ETFs", 3.3),
    "XLK": M("Technology Select Sector", "ETFs", 0.7),
    "SMH": M("VanEck Semiconductor", "ETFs", 0.4, news="semiconductor stocks SMH"),
    "USO": M("United States Oil Fund", "ETFs", 0.0, news="oil price USO"),
    "IBIT": M("iShares Bitcoin Trust", "ETFs", 0.0, news="Bitcoin ETF IBIT"),
    "TSLA": M("Tesla", "Stocks", 0.0, news="Tesla TSLA stock"),
    "NVDA": M("NVIDIA", "Stocks", 0.03, news="Nvidia NVDA stock"),
    "AAPL": M("Apple", "Stocks", 0.4, news="Apple AAPL stock"),
    "MSFT": M("Microsoft", "Stocks", 0.7, news="Microsoft MSFT stock"),
    "AMZN": M("Amazon", "Stocks", 0.0, news="Amazon AMZN stock"),
    "META": M("Meta Platforms", "Stocks", 0.3, news="Meta META stock"),
    "GOOGL": M("Alphabet", "Stocks", 0.3, news="Alphabet Google GOOGL stock"),
    "AMD": M("AMD", "Stocks", 0.0, news="AMD stock"),
    "NFLX": M("Netflix", "Stocks", 0.0, news="Netflix NFLX stock"),
    "PLTR": M("Palantir", "Stocks", 0.0, news="Palantir PLTR stock"),
    "COIN": M("Coinbase", "Stocks", 0.0, news="Coinbase COIN stock"),
    "MSTR": M("Strategy (MSTR)", "Stocks", 0.0, news="MicroStrategy MSTR stock"),
    "GME": M("GameStop", "Stocks", 0.0, news="GameStop GME stock"),
    "BTC": M("Bitcoin options (Deribit)", "Crypto", 0.0, candle="BTC-USD", news="Bitcoin BTC price"),
    "ETH": M("Ethereum options (Deribit)", "Crypto", 0.0, candle="ETH-USD", news="Ethereum ETH price"),
}
NAVS = ["Options", "GEX by Expiry", "Profiles", "Ladder & Levels", "Squeeze Screener", "Surfaces",
        "News & Calendar", "Replay & Sim", "Data & Model"]
EXP_MODES = ["Auto: next N expiries", "Auto: within N days", "Manual"]
LEVEL_OPTIONS = ["Spot", "Flip", "Call wall", "Put wall", "Max pain", "Expected move"]
METRICS = {"GEX": "gex", "DEX": "dex", "VEX": "vex", "CHEX": "chex", "OI": "oi", "Volume": "vol"}
PANEL_OPTIONS = ["VEX", "CHEX", "DEX", "GEX", "OI", "Volume"]
TF = {"1m": "5d", "5m": "30d", "15m": "60d", "1h": "180d"}
TZS = ["UTC", "America/New_York", "Europe/London", "Asia/Manila", "Asia/Singapore", "Asia/Dubai", "Asia/Tokyo", "Australia/Sydney"]
MODELS = {
    "Standard": "Dealers long calls, short puts (classic GEX assumption).",
    "Crypto retail": "Customers net long calls, so dealers are short calls and long puts (common crypto view).",
    "Flow-adjusted": "Standard, plus an estimate of today's customer flow (last trade vs bid/ask). Experimental.",
    "Absolute": "Unsigned gamma: shows where gamma is concentrated, not direction.",
}
THEMES = {
    "Midnight": dict(col_up="#22c55e", col_down="#ef4444", col_accent="#3b82f6", col_flip="#f59e0b", col_bg="#0b0f17", col_panel="#121a2b"),
    "Neon": dict(col_up="#00ffa3", col_down="#ff3d81", col_accent="#7c3aed", col_flip="#fde047", col_bg="#07070d", col_panel="#12121f"),
    "Ocean": dict(col_up="#2dd4bf", col_down="#fb7185", col_accent="#38bdf8", col_flip="#fbbf24", col_bg="#06121c", col_panel="#0e2233"),
    "Colorblind": dict(col_up="#3b82f6", col_down="#f59e0b", col_accent="#a78bfa", col_flip="#e5e7eb", col_bg="#0b0f17", col_panel="#121a2b"),
    "Graphite": dict(col_up="#84cc16", col_down="#f43f5e", col_accent="#94a3b8", col_flip="#facc15", col_bg="#111111", col_panel="#1b1b1b"),
}
DEFAULTS = dict(
    market_sym="QQQ", custom_sym="", nav="Options", live_on=False, live_sec=30,
    n_exp=4, exp_mode=EXP_MODES[0], exp_n=3, exp_days=14, exp_manual=[],
    model="Standard", flow_w=0.5, smooth=True, rate=4.0, win_pct=8, max_strikes=31,
    theme_pick="Midnight", h_main=520, orient="Horizontal", show_kpis=True, show_chips=True, tz="UTC",
    c_levels=["Spot", "Flip", "Call wall", "Put wall", "Max pain"], c_panels=["VEX", "DEX"],
    c_watch=["SPY", "QQQ", "^SPX", "^NDX", "IWM", "TSLA", "NVDA", "BTC"],
    cal_impact=["High", "Medium"], cal_country=["USD"], use_mt5=False,
    **THEMES["Midnight"],
)
CHOICES = dict(market_sym=list(MARKETS), nav=NAVS, exp_mode=EXP_MODES, model=list(MODELS), theme_pick=list(THEMES),
               orient=["Horizontal", "Vertical"], tz=TZS)
TRANSIENT = dict(rp_play=False, rp_speed=1, rp_i=0, rp_prev=0, rp_tf="5m", sim_mode="Replay", sim_usd=1000.0,
                 sim_sl=1.0, sim_tp=2.0, sim_px=0.0, sim_t=None, rp_key=None, rp_levels=True,
                 _prev_exps=None, _saved=None, scan=None, scan_t=None)
SETTINGS_FILE = Path(__file__).with_name("market_terminal_settings.json") if "__file__" in globals() else Path("market_terminal_settings.json")

CSS = """
<style>
.stApp { background:__BG__; color:#d6dbe6; font-family:Inter,"Segoe UI",system-ui,sans-serif; }
section[data-testid="stSidebar"] { background:linear-gradient(180deg,__PANEL__ 0%,__BG__ 100%); border-right:1px solid #1d2638; }
section[data-testid="stSidebar"] details { border:1px solid #1f2a40; border-radius:12px; background:rgba(255,255,255,.02); margin-bottom:10px; }
section[data-testid="stSidebar"] details summary { font-weight:650; font-size:12px; letter-spacing:.1em; text-transform:uppercase; color:__ACC__; }
section[data-testid="stSidebar"] label p { font-size:12.5px; color:#aab4c8 !important; }
header[data-testid="stHeader"] { background:transparent; }
#MainMenu, footer { visibility:hidden; }
.block-container { padding-top:1rem; max-width:1800px; }
h1,h2,h3,h4, label, .stMarkdown p, .stCaption, [data-testid="stWidgetLabel"] p { color:#d6dbe6 !important; }
.brand { font-size:21px; font-weight:750; letter-spacing:-.02em; color:#f1f5f9; padding-top:3px; white-space:nowrap; }
.brand i { color:__ACC__; font-style:normal; }
.sbbrand { font-size:19px; font-weight:700; color:#f1f5f9; margin-bottom:4px; }
.livepill { display:inline-flex; align-items:center; gap:7px; padding:3px 11px; border-radius:999px; font-size:11px;
  letter-spacing:.08em; border:1px solid #1f2a40; color:#c9d1e0; margin-bottom:10px; }
.dot { width:8px; height:8px; border-radius:50%; background:__UP__; box-shadow:0 0 6px __UP__; }
.dot.off { background:#64748b; box-shadow:none; }
.tapebar { display:flex; gap:22px; align-items:center; overflow:hidden; white-space:nowrap; padding:7px 14px; margin:2px 0 8px;
  background:__PANEL__; border:1px solid #1f2a40; border-radius:10px; font-size:12.5px; font-variant-numeric:tabular-nums; }
.tapebar b { color:#f1f5f9; margin-right:5px; } .tapebar .st { margin-left:auto; color:#9aa5bd; font-size:11.5px; letter-spacing:.06em; }
.kpi-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:10px; margin:8px 0 10px; }
.kpi { background:__PANEL__; border:1px solid #1f2a40; border-radius:12px; padding:10px 14px; }
.kpi .l { font-size:11px; letter-spacing:.09em; text-transform:uppercase; color:#7f8aa3; }
.kpi .v { font-size:21px; font-weight:650; margin-top:3px; font-variant-numeric:tabular-nums; white-space:nowrap; }
.kpi .s { font-size:12px; color:#7f8aa3; margin-top:1px; }
.pos { color:__UP__; } .neg { color:__DOWN__; } .neu { color:#f1f5f9; } .amb { color:__FLIP__; } .acc { color:__ACC__; }
.chips { display:flex; gap:8px; flex-wrap:wrap; margin:2px 0 12px; }
.chip { background:__PANEL__; border:1px solid #1f2a40; border-radius:10px; padding:5px 10px; font-size:12px; }
.chip.on { border-color:__ACC__; } .chip b { color:#f1f5f9; } .chip span { color:#7f8aa3; }
.chip.warn { border-color:__DOWN__; }
.panel-title { font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#7f8aa3; margin:6px 0 2px; }
div[data-testid="stExpander"] { border:1px solid #1d2638; border-radius:10px; background:__PANEL__; }
.stButton button, button[data-testid="stBaseButton-secondary"], button[data-testid="stBaseButton-primary"] { border-radius:9px; min-height:34px; font-size:13px; }
.stButton button[kind="secondary"], button[data-testid="stBaseButton-secondary"] { background:__PANEL__; border:1px solid #1f2a40; color:#c9d1e0; }
.stButton button[kind="primary"], button[data-testid="stBaseButton-primary"] { background:__ACC__; border:1px solid __ACC__; color:#fff; }
.walls { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:8px; }
.wh { font-size:11px; letter-spacing:.1em; text-transform:uppercase; color:#7f8aa3; margin-bottom:4px; }
.wall { display:grid; grid-template-columns:30px 36px 1fr auto; gap:4px; font-size:12px; padding:6px 7px; background:rgba(255,255,255,.03);
  border-left:2px solid #1f2a40; margin-bottom:5px; border-radius:4px; font-variant-numeric:tabular-nums; }
.wall .ar { font-size:9px; letter-spacing:-1px; } .wall .wv { color:#9aa5bd; } .wall .wp { font-weight:650; }
.brief { background:__PANEL__; border:1px solid #1f2a40; border-radius:12px; padding:12px 14px; margin-top:8px; }
.brief .bt { font-size:17px; font-weight:650; margin:6px 0; color:#f1f5f9; }
.brief .bs { font-size:13px; color:#b6bfd1; line-height:1.5; } .brief .bn { font-size:11px; color:#6b7691; margin-top:8px; }
.ladder { max-height:660px; overflow:auto; border:1px solid #1f2a40; border-radius:10px; }
.lrow { display:grid; grid-template-columns:110px 1fr 1fr 90px 90px; font-size:12px; align-items:center;
  border-bottom:1px solid #141c2e; font-variant-numeric:tabular-nums; }
.lrow > div { padding:4px 8px; position:relative; }
.lhead { position:sticky; top:0; background:__PANEL__; color:#7f8aa3; font-size:11px; letter-spacing:.08em; text-transform:uppercase; z-index:2; }
.lbar { position:absolute; top:3px; bottom:3px; opacity:.38; border-radius:3px; }
.lrow.spot { background:rgba(59,130,246,.16); }
.tag { font-size:9px; padding:1px 5px; border-radius:6px; background:rgba(255,255,255,.08); margin-left:4px; }
.cal { width:100%; border-collapse:collapse; font-size:12.5px; font-variant-numeric:tabular-nums; }
.cal th { text-align:left; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:#7f8aa3; padding:6px 8px; border-bottom:1px solid #1f2a40; }
.cal td { padding:7px 8px; border-bottom:1px solid #141c2e; }
.cal tr.key td { background:rgba(255,255,255,.04); }
.cal tr.past td { opacity:.45; }
.imp { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
.keytag { font-size:9px; padding:1px 6px; border-radius:6px; background:__ACC__; color:#fff; margin-left:6px; letter-spacing:.06em; }
.news a { color:#e2e8f0; text-decoration:none; } .news a:hover { color:__ACC__; }
.news .item { padding:9px 2px; border-bottom:1px solid #141c2e; font-size:13.5px; line-height:1.4; }
.news .meta { color:#7f8aa3; font-size:11.5px; margin-top:2px; }
</style>
"""


# ============================================================ settings
def new_sim():
    return dict(bal=10000.0, pos=0.0, entry=0.0, sl=None, tp=None, log=[], marks=[], fee=0.0005)


def init_settings():
    if S.get("_init"):
        return
    saved = {}
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        pass
    for k, v in DEFAULTS.items():
        val = saved.get(k, v)
        if isinstance(v, bool) != isinstance(val, bool) or isinstance(v, list) != isinstance(val, list):
            val = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            try:
                val = type(v)(val)
            except Exception:
                val = v
        S[k] = val
    for k, opts in CHOICES.items():
        if S[k] not in opts:
            S[k] = DEFAULTS[k]
    S["c_levels"] = [x for x in S["c_levels"] if x in LEVEL_OPTIONS]
    S["c_panels"] = [x for x in S["c_panels"] if x in PANEL_OPTIONS][:3]
    S["c_watch"] = [x for x in S["c_watch"] if x in MARKETS][:9]
    S["cal_impact"] = [x for x in S["cal_impact"] if x in ("High", "Medium", "Low")]
    for k, v in TRANSIENT.items():
        S[k] = v
    S["sim"] = new_sim()
    S["_init"] = True


def save_settings():
    try:
        data = json.dumps({k: S.get(k, v) for k, v in DEFAULTS.items()}, sort_keys=True)
        if S.get("_saved") != data:
            SETTINGS_FILE.write_text(data)
            S["_saved"] = data
    except Exception:
        pass


def apply_theme():
    for k, v in THEMES[S["theme_pick"]].items():
        S[k] = v


def pal():
    return dict(up=S["col_up"], dn=S["col_down"], acc=S["col_accent"], flip=S["col_flip"], panel=S["col_panel"], bg=S["col_bg"])


def rgba(h, a):
    h = h.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{a})"


def set_sym(s):
    S["market_sym"] = s
    S["custom_sym"] = ""


def cur_sym():
    return S["custom_sym"].strip().upper() or S["market_sym"]


def meta_of(sym):
    return MARKETS.get(sym) or M(sym, "Custom", 0.5)


init_settings()
css = CSS
for token, key in (("__BG__", "col_bg"), ("__PANEL__", "col_panel"), ("__UP__", "col_up"),
                   ("__DOWN__", "col_down"), ("__ACC__", "col_accent"), ("__FLIP__", "col_flip")):
    css = css.replace(token, S[key])
st.markdown(css, unsafe_allow_html=True)


# ============================================================ helpers
def fmt_m(v):
    return f"{v / 1000:+,.2f}B" if abs(v) >= 1000 else f"{v:+,.1f}M"


def fmt_abs(v):
    return f"{v / 1000:,.2f}B" if abs(v) >= 1000 else f"{v:,.1f}M"


def fmt_k(k):
    return f"{k:,.2f}".rstrip("0").rstrip(".")


def fmt_px(v):
    return f"{v:,.2f}" if v >= 1 else f"{v:.5f}"


def cd(secs):
    secs = max(int(secs), 0)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def ago(ts):
    s = max(time.time() - ts, 0)
    return f"{int(s // 60)}m ago" if s < 3600 else f"{int(s // 3600)}h ago" if s < 86400 else f"{int(s // 86400)}d ago"


def kpi(label, value, sub="", tone="neu"):
    return f'<div class="kpi"><div class="l">{label}</div><div class="v {tone}">{value}</div><div class="s">{sub}</div></div>'


def pills(key, options, fmt=str):
    if S.get(key) not in options:
        S[key] = S.get(f"_{key}_last", options[0])
        if S[key] not in options:
            S[key] = options[0]
    sc = getattr(st, "segmented_control", None)
    if sc:
        val = sc(key, options, key=key, label_visibility="collapsed", format_func=fmt)
    else:
        val = st.radio(key, options, key=key, horizontal=True, label_visibility="collapsed", format_func=fmt)
    if val in options:
        S[f"_{key}_last"] = val
        return val
    return S[key]


def style(fig, h=500, **kw):
    layout = dict(
        template="plotly_dark", height=h, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, Segoe UI, Arial, sans-serif", size=12, color="#c9d1e0"),
        margin=dict(l=10, r=10, t=26, b=10), hoverlabel=dict(bgcolor="#121a2b", font_size=12),
        legend=dict(orientation="h", y=1.06, x=0),
    )
    layout.update(kw)
    fig.update_layout(**layout)
    fig.update_xaxes(gridcolor="#1a2236", zeroline=False)
    fig.update_yaxes(gridcolor="#1a2236", zeroline=False)
    return fig


def show(fig, key):
    try:
        st.plotly_chart(fig, width="stretch", key=key, config=PLOT_CFG)
    except Exception:
        st.plotly_chart(fig, use_container_width=True, key=key, config=PLOT_CFG)


def run_fragment(fn, every):
    frag = getattr(st, "fragment", None)
    if frag is None:
        fn()
    else:
        frag(run_every=every)(fn)()


# ============================================================ math
def erf_np(x):
    s = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = ((((1.061405429 * t - 1.453152027) * t + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t
    return s * (1.0 - poly * np.exp(-x * x))


def ncdf(x):
    return 0.5 * (1.0 + erf_np(x / math.sqrt(2.0)))


def bs_price(S_, K, T, iv, side, r, q):
    sq = iv * np.sqrt(T)
    d1 = (np.log(S_ / K) + (r - q + 0.5 * iv**2) * T) / sq
    d2 = d1 - sq
    dr, dq = np.exp(-r * T), np.exp(-q * T)
    call = S_ * dq * ncdf(d1) - K * dr * ncdf(d2)
    put = K * dr * ncdf(-d2) - S_ * dq * ncdf(-d1)
    return np.where(side == 1, call, put)


def implied_vol(price, S_, K, T, side, r, q):
    lo = np.full(price.shape, 0.01)
    hi = np.full(price.shape, 3.0)
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        too_high = bs_price(S_, K, T, mid, side, r, q) > price
        hi = np.where(too_high, mid, hi)
        lo = np.where(too_high, lo, mid)
    return 0.5 * (lo + hi)


def exp_kind(ts):
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    if d.weekday() == 4 and (d + timedelta(days=7)).month != d.month:
        return "Quarterly" if d.month in (3, 6, 9, 12) else "Monthly"
    return "Weekly" if d.weekday() == 4 else "Daily"


def http_get(url, params=None, timeout=10):
    r = requests.get(url, params=params, timeout=timeout, headers=UA)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from {url.split('/')[2]}")
    return r.json()


# ============================================================ data loaders
def _chain(sym, exp):
    try:
        ch = yf.Ticker(sym).option_chain(exp)
        return exp, ch.calls, ch.puts
    except Exception:
        return exp, None, None


COLS = ["strike", "openInterest", "volume", "impliedVolatility", "bid", "ask", "lastPrice"]
REN = {"openInterest": "oi", "volume": "vol", "impliedVolatility": "yiv", "lastPrice": "last_px"}


def load_yahoo(sym, n_exp):
    t = yf.Ticker(sym)
    exps = list(t.options[:n_exp])
    if not exps:
        raise ValueError(f"{sym} has no listed options on Yahoo Finance")
    try:
        spot = float(t.fast_info["last_price"])
    except Exception:
        spot = float(t.history(period="5d")["Close"].iloc[-1])
    with ThreadPoolExecutor(max_workers=min(6, len(exps))) as ex:
        results = list(ex.map(lambda e: _chain(sym, e), exps))
    frames = []
    for exp, calls, puts in results:
        if calls is None:
            continue
        ts = pd.Timestamp(f"{exp} 16:00", tz="America/New_York").timestamp()
        for df, side in ((calls, 1), (puts, -1)):
            d = df[COLS].rename(columns=REN)
            d["side"], d["exp_ts"], d["expiry"], d["kind"], d["mult"] = side, ts, exp, exp_kind(ts), 100.0
            frames.append(d)
    if not frames:
        raise ValueError("no option chains could be downloaded")
    return spot, pd.concat(frames, ignore_index=True)


def load_deribit(ccy, n_exp):
    base = "https://www.deribit.com/api/v2/public/"
    spot = float(http_get(base + "get_index_price", {"index_name": f"{ccy.lower()}_usd"})["result"]["index_price"])
    res = http_get(base + "get_book_summary_by_currency", {"currency": ccy, "kind": "option"}, 30)["result"]
    rows = []
    for it in res:
        try:
            _, dstr, k, cp = it["instrument_name"].split("-")
            ts = (pd.to_datetime(dstr, format="%d%b%y").tz_localize("UTC") + pd.Timedelta(hours=8)).timestamp()
            rows.append(dict(
                strike=float(k), oi=float(it.get("open_interest") or 0), vol=float(it.get("volume") or 0),
                yiv=float(it.get("mark_iv") or 0) / 100, bid=float(it.get("bid_price") or 0),
                ask=float(it.get("ask_price") or 0), last_px=float(it.get("last") or 0), side=1 if cp == "C" else -1,
                exp_ts=ts, expiry=datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                kind=exp_kind(ts), mult=1.0))
        except Exception:
            continue
    d = pd.DataFrame(rows)
    if d.empty:
        raise ValueError("Deribit returned no options")
    d = d[d["exp_ts"] > time.time()]
    keep = sorted(d["exp_ts"].unique())[:n_exp]
    return spot, d[d["exp_ts"].isin(keep)].reset_index(drop=True)


@st.cache_data(ttl=120, show_spinner="Loading options chain...", max_entries=12)
def load_chain(sym, n_exp):
    spot, d = load_deribit(sym, n_exp) if meta_of(sym)["group"] == "Crypto" else load_yahoo(sym, n_exp)
    d[["oi", "vol", "bid", "ask", "last_px"]] = d[["oi", "vol", "bid", "ask", "last_px"]].fillna(0)
    return spot, d


@st.cache_data(ttl=60, show_spinner=False)
def fetch_quotes(syms):
    def one(s):
        cs = meta_of(s).get("candle") or s
        try:
            fi = yf.Ticker(cs).fast_info
            last, prev = float(fi["last_price"]), float(fi["previous_close"])
            return s, last, (last / prev - 1) * 100
        except Exception:
            return s, None, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(one, syms))


@st.cache_data(ttl=60, show_spinner=False)
def load_candles(csym, tf):
    h = yf.Ticker(csym).history(period=TF[tf], interval="60m" if tf == "1h" else tf)
    if h is None or h.empty:
        return pd.DataFrame()
    idx = h.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    k = pd.DataFrame({"t": idx, "o": h["Open"].values, "h": h["High"].values, "l": h["Low"].values,
                      "c": h["Close"].values, "v": h["Volume"].values}).tail(700).reset_index(drop=True)
    k["lab"] = k["t"].dt.strftime("%m-%d %H:%M")
    return k


@st.cache_data(ttl=900, show_spinner=False)
def load_calendar():
    rows = []
    for name in ("thisweek", "nextweek"):
        try:
            for e in http_get(f"https://nfs.faireconomy.media/ff_calendar_{name}.json", None, 4):
                try:
                    rows.append(dict(ts=pd.to_datetime(e["date"], utc=True).timestamp(), title=e.get("title", ""),
                                     country=e.get("country", ""), impact=e.get("impact", ""),
                                     forecast=e.get("forecast", "") or "", previous=e.get("previous", "") or ""))
                except Exception:
                    continue
        except Exception:
            continue
    return pd.DataFrame(rows, columns=["ts", "title", "country", "impact", "forecast", "previous"])


@st.cache_data(ttl=600, show_spinner=False)
def load_headlines(query, n=14):
    r = requests.get("https://news.google.com/rss/search", timeout=8, headers=UA,
                     params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        try:
            ts = pd.to_datetime(it.findtext("pubDate"), utc=True).timestamp()
        except Exception:
            ts = 0
        items.append(dict(title=it.findtext("title") or "", link=it.findtext("link") or "", ts=ts,
                          source=it.findtext("source") or ""))
    items.sort(key=lambda x: -x["ts"])
    return items[:n]


# ============================================================ options model
def smooth_iv(d, spot):
    out = d["iv"].copy()
    for _, g in d.groupby("expiry"):
        otm = g[((g["side"] == 1) & (g["strike"] >= spot)) | ((g["side"] == -1) & (g["strike"] < spot))]
        otm = otm[otm["strike"].between(spot * 0.6, spot * 1.4)]
        if len(otm) < 8:
            continue
        x = np.log(otm["strike"].values / spot)
        y = otm["iv"].values
        w = np.sqrt(otm["oi"].values + 1.0)
        keep = np.ones(len(x), bool)
        coef = None
        for _ in range(3):
            coef = np.polyfit(x[keep], y[keep], min(4, keep.sum() - 2), w=w[keep])
            res = y - np.polyval(coef, x)
            mad = np.median(np.abs(res[keep] - np.median(res[keep]))) + 1e-6
            new = np.abs(res) < 4 * 1.4826 * mad
            if new.sum() < 8 or (new == keep).all():
                break
            keep = new
        xs = np.clip(np.log(g["strike"].values / spot), x.min(), x.max())
        out.loc[g.index] = np.clip(np.polyval(coef, xs), 0.03, 5.0)
    return out


@st.cache_data(show_spinner=False, max_entries=10)
def prepare_iv(raw, spot, r, q, smooth, bucket):
    now = bucket * 60.0
    d = raw[raw["exp_ts"] > now].copy()
    d["T"] = np.maximum((d["exp_ts"] - now) / (365 * 86400), 0.25 / 365)
    d["dte"] = (d["exp_ts"] - now) / 86400
    d["mid"] = (d["bid"] + d["ask"]) / 2
    spread = d["ask"] - d["bid"]
    safe_mid = d["mid"].where(d["mid"] > 0)
    qok = (d["bid"] > 0) & (d["ask"] >= d["bid"]) & (d["mid"] >= 0.05) & ((spread / safe_mid) <= 0.6)
    m = (qok & (d["mult"] == 100)).values
    iv_q = np.full(len(d), np.nan)
    if m.any():
        K, T, sd, px = d["strike"].values[m], d["T"].values[m], d["side"].values[m], d["mid"].values[m]
        iv = implied_vol(px, spot, K, T, sd, r, q)
        err = np.abs(bs_price(spot, K, T, iv, sd, r, q) - px)
        iv[err > 0.03 + 0.02 * px] = np.nan
        iv_q[m] = iv
    yiv = d["yiv"].values
    use_q = np.isfinite(iv_q) & (iv_q > 0.03) & (iv_q < 3.0)
    use_y = (~use_q) & (yiv > 0.03) & (yiv < 5.0)
    d["iv"] = np.where(use_q, iv_q, np.where(use_y, yiv, np.nan))
    d["iv_src"] = np.where(use_q, "quote", np.where(use_y, "feed", "none"))
    d = d.dropna(subset=["iv"]).reset_index(drop=True)
    if d.empty:
        return d
    d["iv_s"] = smooth_iv(d, spot) if smooth else d["iv"]
    mid = (d["bid"] + d["ask"]) / 2
    half = np.maximum(0.5 * (d["ask"] - d["bid"]), 1e-9)
    ok = (d["bid"] > 0) & (d["ask"] > 0) & (d["last_px"] > 0) & (d["vol"] > 0)
    d["flow"] = np.where(ok, np.clip((d["last_px"] - mid) / half, -1, 1), 0.0)
    return d


def build(d, spot, r, q, model, flow_w):
    K, T, iv = d["strike"].values, d["T"].values, d["iv_s"].values
    sq = iv * np.sqrt(T)
    d1 = (np.log(spot / K) + (r - q + 0.5 * iv**2) * T) / sq
    d2 = d1 - sq
    pdf = np.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)
    eq = np.exp(-q * T)
    gamma = eq * pdf / (spot * sq)
    side = d["side"].values
    delta = np.where(side == 1, eq * ncdf(d1), eq * (ncdf(d1) - 1))
    vanna = -eq * pdf * d2 / iv
    core = eq * pdf * (2 * (r - q) * T - d2 * sq) / (2 * T * sq)
    charm = np.where(side == 1, q * eq * ncdf(d1) - core, -q * eq * ncdf(-d1) - core)
    oi, vol, mult = d["oi"].values.astype(float), d["vol"].values.astype(float), d["mult"].values.astype(float)
    if model == "Crypto retail":
        pos = -side * oi
    elif model == "Flow-adjusted":
        pos = side * oi - d["flow"].values * vol * flow_w
    elif model == "Absolute":
        pos = oi
    else:
        pos = side * oi
    out = d.copy()
    out["pos"] = pos
    out["gex"] = pos * mult * gamma * spot**2 * 0.01 / 1e6
    out["dex"] = pos * mult * delta * spot / 1e6
    out["vex"] = pos * mult * vanna * spot * 0.01 / 1e6
    out["chex"] = pos * mult * charm / 365 * spot / 1e6
    return out


def exposure_profile(d, spot, r, q, col="gex", span=0.12, n=101):
    g = spot * np.linspace(1 - span, 1 + span, n)[:, None]
    K, T, iv, pos, side, mult = (d[c].values[None, :] for c in ("strike", "T", "iv_s", "pos", "side", "mult"))
    sq = iv * np.sqrt(T)
    d1 = (np.log(g / K) + (r - q + 0.5 * iv**2) * T) / sq
    d2 = d1 - sq
    pdf = np.exp(-0.5 * d1**2) / math.sqrt(2 * math.pi)
    eq = np.exp(-q * T)
    if col == "gex":
        val = eq * pdf / (g * sq) * g**2 * 0.01
    elif col == "dex":
        val = np.where(side == 1, eq * ncdf(d1), eq * (ncdf(d1) - 1)) * g
    elif col == "vex":
        val = -eq * pdf * d2 / iv * g * 0.01
    else:
        core = eq * pdf * (2 * (r - q) * T - d2 * sq) / (2 * T * sq)
        val = np.where(side == 1, q * eq * ncdf(d1) - core, -q * eq * ncdf(-d1) - core) / 365 * g
    return g[:, 0], (val * pos * mult).sum(axis=1) / 1e6


def find_flip(xs, tot, spot):
    flip = None
    for i in range(len(xs) - 1):
        if tot[i] == 0 or tot[i] * tot[i + 1] < 0:
            c = xs[i] if tot[i + 1] == tot[i] else xs[i] - tot[i] * (xs[i + 1] - xs[i]) / (tot[i + 1] - tot[i])
            if flip is None or abs(c - spot) < abs(flip - spot):
                flip = c
    return flip


def max_pain(d):
    ks = np.sort(d["strike"].unique())
    c = d[d["side"] == 1].groupby("strike")["oi"].sum().reindex(ks, fill_value=0).values
    p = d[d["side"] == -1].groupby("strike")["oi"].sum().reindex(ks, fill_value=0).values
    pain = [(c * np.maximum(k - ks, 0)).sum() + (p * np.maximum(ks - k, 0)).sum() for k in ks]
    return float(ks[int(np.argmin(pain))])


def find_walls(d, spot, vis):
    g = d[d["strike"].isin(vis)]
    c = g[g["side"] == 1].groupby("strike")["gex"].sum()
    p = g[g["side"] == -1].groupby("strike")["gex"].sum()
    ca, pa = c[c.index >= spot], p[p.index <= spot]
    cw = float(ca.abs().idxmax()) if len(ca) and ca.abs().max() > 0 else None
    pw = float(pa.abs().idxmax()) if len(pa) and pa.abs().max() > 0 else None
    return cw, pw


def atm_iv(g, spot):
    idx = (g["strike"] - spot).abs().values.argsort()[:4]
    return float(g.iloc[idx]["iv_s"].mean())


@st.cache_data(ttl=120, show_spinner=False, max_entries=8)
def compute_ctx(sym, n_exp, model, flow_w, smooth, r, q, win_pct, max_strikes, exp_mode, exp_n, exp_days, manual, bucket):
    spot, raw = load_chain(sym, n_exp)
    now = bucket * 60.0
    live = raw[raw["exp_ts"] > now].sort_values("exp_ts")
    exps = list(live["expiry"].unique())
    if not exps:
        return None
    tsmap = live.groupby("expiry")["exp_ts"].first().to_dict()
    dmap = {e: (t - now) / 86400 for e, t in tsmap.items()}
    if exp_mode == "Manual":
        sel = [e for e in manual if e in exps] or exps[:1]
    elif exp_mode == "Auto: within N days":
        sel = [e for e in exps if dmap[e] <= exp_days] or exps[:1]
    else:
        sel = exps[:exp_n]
    d = prepare_iv(raw, spot, r, q, smooth, bucket)
    if d.empty:
        return None
    df_all = build(d, spot, r, q, model, flow_w)
    d_sel = df_all[df_all["expiry"].isin(sel)]
    if d_sel.empty:
        return None
    ks = sorted(df_all["strike"].unique())
    cand = [k for k in ks if abs(k / spot - 1) <= win_pct / 100]
    if len(cand) > max_strikes:
        cand = sorted(cand, key=lambda k: abs(k - spot))[:max_strikes]
    if len(cand) < 5:
        cand = sorted(ks, key=lambda k: abs(k - spot))[:11]
    vis = set(cand)
    xs, prof = exposure_profile(d_sel, spot, r, q, "gex")
    flip = None if model == "Absolute" else find_flip(xs, prof, spot)
    cw, pw = find_walls(d_sel, spot, vis)
    first = sorted(sel, key=lambda e: tsmap[e])[0]
    g0 = d_sel[d_sel["expiry"] == first]
    em = spot * atm_iv(g0, spot) * math.sqrt(float(g0["T"].iloc[0]))
    quote_share = float((df_all["iv_src"] == "quote").mean())
    return dict(sym=sym, spot=spot, r=r, q=q, exps=exps, sel=sel, tsmap=tsmap, dmap=dmap, df_all=df_all, d_sel=d_sel,
                vis=vis, flip=flip, call_wall=cw, put_wall=pw, pain=max_pain(d_sel), net=float(d_sel["gex"].sum()),
                em=em, first=first, quote_share=quote_share)


def get_mt5_price(symbol):
    try:
        import MetaTrader5 as mt5

        if not mt5.initialize():
            return None
        tick = mt5.symbol_info_tick(symbol)
        return (tick.bid + tick.ask) / 2 if tick else None
    except Exception:
        return None


def get_ratio(ref_price, sym):
    price = get_mt5_price(S.get(f"cfd_{sym}", "")) if S["use_mt5"] else None
    if not price:
        price = S.get(f"cp_{sym}", 0.0)
    return (price / ref_price) if price and price > 0 and ref_price else 1.0


def unit_of(sym, ratio):
    return S.get(f"cfd_{sym}", sym) if ratio != 1.0 else sym.lstrip("^")


def get_ctx():
    sym = cur_sym()
    meta = meta_of(sym)
    crypto = meta["group"] == "Crypto"
    d = compute_ctx(
        sym, S["n_exp"], S["model"], S["flow_w"], S["smooth"], S["rate"] / 100,
        S.get(f"q_{sym}", meta["div"]) / 100, S["win_pct"] * (2 if crypto else 1), S["max_strikes"],
        S["exp_mode"], S["exp_n"], S["exp_days"], tuple(S["exp_manual"]), int(time.time() // 60))
    if d is None:
        return None
    c = SimpleNamespace(**d)
    c.meta = meta
    c.ratio = get_ratio(c.spot, sym)
    c.unit = unit_of(sym, c.ratio)
    prev = S.get("_prev_exps")
    if prev:
        gone = [e for e in prev if e not in c.exps]
        if gone:
            try:
                st.toast(f"Expired: {', '.join(gone)}. Rolled forward to {c.exps[0]}", icon="⏱️")
            except Exception:
                pass
    S["_prev_exps"] = c.exps
    return c


# ============================================================ options figures
def chart_levels(c):
    ch = S["c_levels"]
    p = pal()
    out = []
    if "Spot" in ch:
        out.append((c.spot, "Spot", p["acc"], "dash"))
    if "Flip" in ch and c.flip:
        out.append((c.flip, "Flip", p["flip"], "dash"))
    if "Call wall" in ch and c.call_wall:
        out.append((c.call_wall, "Call wall", p["up"], "dash"))
    if "Put wall" in ch and c.put_wall:
        out.append((c.put_wall, "Put wall", p["dn"], "dash"))
    if "Max pain" in ch:
        out.append((c.pain, "Max pain", "#a78bfa", "dot"))
    if "Expected move" in ch:
        out += [(c.spot + c.em, "+1σ", "#94a3b8", "dot"), (c.spot - c.em, "-1σ", "#94a3b8", "dot")]
    return out


def add_level(fig, v, label, color, dash, horiz, **kw):
    a = dict(line_dash=dash, line_color=color, line_width=1, annotation_text=label, annotation_font_color=color,
             annotation_font_size=10)
    if horiz:
        fig.add_hline(y=v, annotation_position="top right", **a, **kw)
    else:
        fig.add_vline(x=v, annotation_position="top", **a, **kw)


def side_sum(c, col, side):
    sub = c.d_sel[c.d_sel["strike"].isin(c.vis)]
    return sub[sub["side"] == side].groupby("strike")[col].sum().reindex(sorted(c.vis), fill_value=0)


def bars_fig(c, metric, h):
    p = pal()
    col = METRICS[metric]
    horiz = S["orient"] == "Horizontal"
    ks = sorted(c.vis)
    axis = [k * c.ratio for k in ks]
    suffix = "" if col in ("oi", "vol") else "M"
    fig = go.Figure()

    def add(name, vals, colors, opacity=0.85):
        hv = ("%{y:,.2f}: %{x:+,.1f}" if horiz else "%{x:,.2f}: %{y:+,.1f}") + suffix
        kw = dict(name=name, marker=dict(color=colors, opacity=opacity), hovertemplate=hv + "<extra>" + name + "</extra>")
        if horiz:
            fig.add_bar(y=axis, x=vals, orientation="h", **kw)
        else:
            fig.add_bar(x=axis, y=vals, **kw)

    if col in ("oi", "vol"):
        add("Calls", side_sum(c, col, 1).values, p["up"])
        add("Puts", -side_sum(c, col, -1).values, p["dn"])
    else:
        net = (side_sum(c, col, 1) + side_sum(c, col, -1)).values
        top3 = set(np.argsort(-np.abs(net))[:3].tolist())
        add("Net", net, [p["up"] if v >= 0 else p["dn"] for v in net], [1.0 if i in top3 else 0.65 for i in range(len(net))])
    for v, label, color, dash in chart_levels(c):
        add_level(fig, v * c.ratio, label, color, dash, horiz)
    vt = metric if col in ("oi", "vol") else f"{metric} ($M)"
    style(fig, h, barmode="relative", bargap=0.2, showlegend=col in ("oi", "vol"),
          xaxis_title=vt if horiz else c.unit, yaxis_title=c.unit if horiz else vt)
    return fig


def profile_fig(c, metric, h):
    p = pal()
    xs, ys = exposure_profile(c.d_sel, c.spot, c.r, c.q, METRICS[metric])
    fig = go.Figure(go.Scatter(x=xs * c.ratio, y=ys, mode="lines", line=dict(color=p["acc"], width=2.5), fill="tozeroy",
                               fillcolor=rgba(p["acc"], 0.15), hovertemplate="%{x:,.2f}: %{y:+.1f}M<extra></extra>"))
    fig.add_hline(y=0, line_color="#94a3b8", line_width=1)
    fig.add_vline(x=c.spot * c.ratio, line_dash="dash", line_color=p["acc"], annotation_text="Spot")
    z = find_flip(xs, ys, c.spot)
    if z:
        fig.add_vline(x=z * c.ratio, line_dash="dash", line_color=p["flip"], annotation_text="Zero", annotation_position="bottom right")
    style(fig, h, xaxis_title=c.unit, yaxis_title=f"Total {metric} ($M)", showlegend=False)
    return fig


def oi_profile_fig(c, h):
    p = pal()
    axis = [k * c.ratio for k in sorted(c.vis)]
    fig = go.Figure()
    fig.add_bar(x=axis, y=side_sum(c, "oi", 1).values, name="Call OI", marker_color=p["up"])
    fig.add_bar(x=axis, y=-side_sum(c, "oi", -1).values, name="Put OI", marker_color=p["dn"])
    fig.add_vline(x=c.spot * c.ratio, line_dash="dash", line_color=p["acc"], annotation_text="Spot")
    style(fig, h, barmode="relative", xaxis_title=c.unit, yaxis_title="Open interest")
    return fig


def oivol_fig(c, h):
    p = pal()
    ks = sorted(c.vis)
    axis = [k * c.ratio for k in ks]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, specs=[[{}], [{"secondary_y": True}]])
    oi_c, oi_p = side_sum(c, "oi", 1), side_sum(c, "oi", -1)
    v_c, v_p = side_sum(c, "vol", 1), side_sum(c, "vol", -1)
    fig.add_bar(x=axis, y=oi_c.values, name="Call OI", marker_color=p["up"], opacity=0.6, row=1, col=1)
    fig.add_bar(x=axis, y=-oi_p.values, name="Put OI", marker_color=p["dn"], opacity=0.6, row=1, col=1)
    fig.add_bar(x=axis, y=v_c.values, name="Call volume", marker_color=p["up"], row=2, col=1)
    fig.add_bar(x=axis, y=-v_p.values, name="Put volume", marker_color=p["dn"], row=2, col=1)
    ratio = ((v_c + v_p) / (oi_c + oi_p).replace(0, np.nan)).fillna(0)
    fig.add_trace(go.Scatter(x=axis, y=ratio.values, name="Volume / OI", mode="lines+markers",
                             line=dict(color=p["flip"], width=1.5), marker=dict(size=4)), row=2, col=1, secondary_y=True)
    fig.add_vline(x=c.spot * c.ratio, line_dash="dash", line_color=p["acc"])
    style(fig, h, barmode="relative")
    fig.update_yaxes(title_text="OI", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Vol / OI", row=2, col=1, secondary_y=True, showgrid=False)
    return fig


def cum_fig(c, metric, h):
    p = pal()
    col = METRICS[metric]
    axis = [k * c.ratio for k in sorted(c.vis)]
    cc, pp = side_sum(c, col, 1), side_sum(c, col, -1)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=axis, y=(cc + pp).cumsum().values, name="Cumulative net", mode="lines",
                             line=dict(color=p["acc"], width=2.5), fill="tozeroy", fillcolor=rgba(p["acc"], 0.12)))
    fig.add_trace(go.Scatter(x=axis, y=cc.cumsum().values, name="Calls", mode="lines", line=dict(color=p["up"], width=1.5)))
    fig.add_trace(go.Scatter(x=axis, y=pp.cumsum().values, name="Puts", mode="lines", line=dict(color=p["dn"], width=1.5)))
    fig.add_hline(y=0, line_color="#94a3b8", line_width=1)
    fig.add_vline(x=c.spot * c.ratio, line_dash="dash", line_color=p["acc"], annotation_text="Spot")
    style(fig, h, xaxis_title=c.unit, yaxis_title=f"Cumulative {metric} ($M)")
    return fig


def expiry_stats(c, col):
    rows = []
    for e, g in c.df_all.groupby("expiry"):
        cl, pt = g[g["side"] == 1], g[g["side"] == -1]
        rows.append(dict(expiry=e, kind=g["kind"].iloc[0], dte=float(g["dte"].iloc[0]), call=cl[col].sum(), put=pt[col].sum(),
                         call_oi=cl["oi"].sum(), put_oi=pt["oi"].sum(), call_vol=cl["vol"].sum(), put_vol=pt["vol"].sum()))
    t = pd.DataFrame(rows).sort_values("dte").reset_index(drop=True)
    t["net"] = t["call"] - t["put"] if col in ("oi", "vol") else t["call"] + t["put"]
    t["share"] = t["net"].abs() / max(t["net"].abs().sum(), 1e-12) * 100
    return t


def expiry_fig(c, metric, t, h):
    p = pal()
    col = METRICS[metric]
    opac = [1.0 if e in c.sel else 0.45 for e in t["expiry"]]
    putv = -t["put"] if col in ("oi", "vol") else t["put"]
    fig = go.Figure()
    fig.add_bar(x=t["expiry"], y=t["call"], name="Calls", marker=dict(color=p["up"], opacity=opac))
    fig.add_bar(x=t["expiry"], y=putv, name="Puts", marker=dict(color=p["dn"], opacity=opac))
    fig.add_trace(go.Scatter(x=t["expiry"], y=t["net"], name="Net", mode="lines+markers",
                             line=dict(color=p["flip"], width=2), marker=dict(size=7)))
    fig.add_hline(y=0, line_color="#94a3b8", line_width=1)
    style(fig, h, barmode="relative", xaxis=dict(type="category"), yaxis_title=metric if col in ("oi", "vol") else f"{metric} ($M)")
    return fig


def heat_fig(c, metric, h):
    p = pal()
    col = {**METRICS, "IV": "iv_s"}[metric]
    d = c.df_all[c.df_all["strike"].isin(c.vis)]
    piv = d.pivot_table(index="strike", columns="expiry", values=col, aggfunc="mean" if metric == "IV" else "sum").sort_index()
    if metric == "IV":
        piv = piv * 100
    else:
        piv["Total"] = piv.sum(axis=1)
    signed = metric in ("GEX", "DEX", "VEX", "CHEX")
    fmt = fmt_m if signed else (lambda v: f"{v:.1f}%") if metric == "IV" else (lambda v: f"{v:,.0f}")
    labels = [("► " if k == min(piv.index, key=lambda x: abs(x - c.spot)) else "") + fmt_k(k * c.ratio) for k in piv.index]
    z = piv.values
    zmax = float(np.nanmax(np.abs(z))) if np.isfinite(z).any() else 1.0
    kw = (dict(zmin=-zmax, zmax=zmax, zmid=0, colorscale=[[0, p["dn"]], [0.5, p["panel"]], [1, p["up"]]]) if signed
          else dict(colorscale="Viridis"))
    if z.size <= 400:
        kw.update(text=[[fmt(v) if pd.notna(v) else "" for v in row] for row in z], texttemplate="%{text}",
                  textfont=dict(size=11, color="#e5e7eb"))
    fig = go.Figure(go.Heatmap(z=z, x=list(piv.columns), y=labels, xgap=2, ygap=2, showscale=False, **kw))
    style(fig, max(h, 24 * len(labels) + 140), xaxis=dict(type="category", side="top"),
          yaxis=dict(type="category", categoryorder="array", categoryarray=labels))
    return fig


def surface_fig(c, kind, h):
    p = pal()
    sub = c.df_all[c.df_all["strike"].between(c.spot * 0.75, c.spot * 1.25)]
    if kind == "IV":
        otm = sub[((sub["side"] == 1) & (sub["strike"] >= c.spot)) | ((sub["side"] == -1) & (sub["strike"] < c.spot))]
        surf = otm.pivot_table(index="dte", columns="strike", values="iv_s", aggfunc="mean") * 100
        surf = surf.sort_index().interpolate(axis=1, limit_direction="both").dropna(axis=1, how="any").dropna(how="all")
        extra, zt = dict(colorscale="Viridis"), "IV %"
    else:
        col = {"GEX": "gex", "VEX": "vex", "DEX": "dex", "Charm": "chex"}[kind]
        surf = sub.pivot_table(index="dte", columns="strike", values=col, aggfunc="sum").sort_index().fillna(0)
        extra = dict(colorscale=[[0, p["dn"]], [0.5, p["panel"]], [1, p["up"]]], cmid=0)
        zt = f"{kind} $M"
    if surf.shape[1] > 40:
        surf = surf.iloc[:, :: int(math.ceil(surf.shape[1] / 40))]
    if len(surf) < 2 or surf.shape[1] < 3:
        return None
    fig = go.Figure(go.Surface(x=surf.columns.values * c.ratio, y=surf.index.values, z=surf.values,
                               colorbar=dict(title=zt, thickness=10), **extra))
    style(fig, h, margin=dict(l=0, r=0, t=10, b=0), scene=dict(
        xaxis_title=c.unit, yaxis_title="Days to expiry", zaxis_title=zt, bgcolor="rgba(0,0,0,0)",
        camera=dict(eye=dict(x=1.6, y=-1.6, z=0.8))))
    return fig


def walls_html(c, col):
    g = c.d_sel[c.d_sel["strike"].isin(c.vis)]

    def top(side):
        s = g[g["side"] == side].groupby("strike")[col].sum()
        return s.reindex(s.abs().sort_values(ascending=False).index)[:3]

    cs, ps = top(1), top(-1)
    mx = max([abs(x) for x in list(cs.values) + list(ps.values)] + [1e-9])

    def rows(s, label, tone):
        out = ""
        for k, v in s.items():
            n = 3 if abs(v) >= 0.66 * mx else 2 if abs(v) >= 0.33 * mx else 1
            out += (f'<div class="wall"><span class="{tone}">{label}</span><span class="{tone} ar">{"▲" * n}</span>'
                    f'<span class="wv">{fmt_m(v)}</span><span class="{tone} wp">{fmt_k(k * c.ratio)}</span></div>')
        return out

    return (f'<div class="walls"><div><div class="wh">Call walls</div>{rows(cs, "Call", "pos")}</div>'
            f'<div><div class="wh">Put walls</div>{rows(ps, "Put", "neg")}</div></div>')


def brief_html(c):
    head = '<div class="panel-title">Signal brief · positioning</div>'
    if S["model"] == "Absolute":
        return f'<div class="brief">{head}<div class="bs">The Absolute model shows concentration only. Switch model for a sentiment read.</div></div>'
    ct = float(c.d_sel[c.d_sel["side"] == 1]["gex"].sum())
    pt = abs(float(c.d_sel[c.d_sel["side"] == -1]["gex"].sum()))
    score = (1 if c.net > 0 else -1) + (1 if ct > pt else -1) + ((1 if c.spot > c.flip else -1) if c.flip else 0)
    word, tone = ("Bullish", "pos") if score >= 2 else ("Bearish", "neg") if score <= -2 else ("Neutral", "amb")
    cmp_txt = "Calls &gt; puts" if ct > pt else "Puts &gt; calls"
    return (f'<div class="brief">{head}<div class="bt">Gamma positioning is <span class="{tone}">{word}</span>.</div>'
            f'<div class="bs">{cmp_txt}. {fmt_abs(ct)} of gamma sits at <span class="pos">call-heavy</span> strikes and '
            f'{fmt_abs(pt)} at <span class="neg">put-heavy</span> strikes.</div>'
            f'<div class="bn">Model-based read of dealer positioning, not a forecast.</div></div>')


def ladder_html(c, col):
    p = pal()
    ks = sorted(c.vis, reverse=True)
    cc, pp = side_sum(c, col, 1), side_sum(c, col, -1)
    sub = c.d_sel[c.d_sel["strike"].isin(c.vis)]
    oi = sub.groupby("strike")["oi"].sum()
    mx = max(float(cc.abs().max()), float(pp.abs().max()), 1e-12)
    near = min(ks, key=lambda k: abs(k - c.spot))
    f = fmt_m if col not in ("oi", "vol") else (lambda v: f"{v:,.0f}")
    rows = '<div class="lrow lhead"><div>Strike</div><div>Calls</div><div>Puts</div><div>Net</div><div>OI</div></div>'
    for k in ks:
        tags = ""
        if c.call_wall and k == c.call_wall:
            tags += '<span class="tag">CALL WALL</span>'
        if c.put_wall and k == c.put_wall:
            tags += '<span class="tag">PUT WALL</span>'
        if k == c.pain:
            tags += '<span class="tag">PAIN</span>'
        cv, pv = float(cc.get(k, 0)), float(pp.get(k, 0))
        net = cv - pv if col in ("oi", "vol") else cv + pv
        rows += (f'<div class="lrow {"spot" if k == near else ""}"><div><b>{fmt_k(k * c.ratio)}</b>{tags}</div>'
                 f'<div><div class="lbar" style="left:0;width:{abs(cv) / mx * 100:.0f}%;background:{p["up"]}"></div>{f(cv)}</div>'
                 f'<div><div class="lbar" style="left:0;width:{abs(pv) / mx * 100:.0f}%;background:{p["dn"]}"></div>{f(pv)}</div>'
                 f'<div>{f(net)}</div><div>{oi.get(k, 0):,.0f}</div></div>')
    return f'<div class="ladder">{rows}</div>'


def levels_table(c):
    rows = [("Spot", c.spot, "Current price")]
    if c.flip:
        rows.append(("Gamma flip", c.flip, "Total GEX crosses zero"))
    if c.call_wall:
        rows.append(("Call wall", c.call_wall, "Largest call GEX above spot"))
    if c.put_wall:
        rows.append(("Put wall", c.put_wall, "Largest put GEX below spot"))
    rows.append(("Max pain", c.pain, "Strike with least option payout"))
    sub = c.d_sel[c.d_sel["strike"].isin(c.vis)]
    net = sub.groupby("strike")["gex"].sum()
    if len(net):
        rows.append(("Top +GEX strike", float(net.idxmax()), fmt_m(float(net.max()))))
        rows.append(("Top -GEX strike", float(net.idxmin()), fmt_m(float(net.min()))))
    oi, vol = sub.groupby("strike")["oi"].sum(), sub.groupby("strike")["vol"].sum()
    if len(oi):
        rows.append(("Top OI strike", float(oi.idxmax()), f"{oi.max():,.0f} contracts"))
    if len(vol) and vol.max() > 0:
        rows.append(("Top volume strike", float(vol.idxmax()), f"{vol.max():,.0f} contracts"))
    ks = sorted(c.vis)
    cum = (side_sum(c, "gex", 1) + side_sum(c, "gex", -1)).cumsum().values
    for i in range(len(ks) - 1):
        if cum[i] * cum[i + 1] < 0:
            rows.append(("Cumulative GEX zero", ks[i], "Cumulative net crosses zero"))
            break
    rows += [("+1σ expected move", c.spot + c.em, f"nearest expiry {c.first}"), ("-1σ expected move", c.spot - c.em, "")]
    t = pd.DataFrame(rows, columns=["Level", "Price", "Note"])
    t["Distance %"] = (t["Price"] / c.spot - 1) * 100
    t["Price"] = t["Price"] * c.ratio
    return t[["Level", "Price", "Distance %", "Note"]]


def analyze_symbol(sym, n_exp, model, flow_w, r):
    meta = meta_of(sym)
    q = S.get(f"q_{sym}", meta["div"]) / 100
    spot, raw = load_chain(sym, n_exp)
    bucket = int(time.time() // 60)
    d = prepare_iv(raw, spot, r, q, True, bucket)
    if d.empty:
        return []
    da, dm = build(d, spot, r, q, "Absolute", 0.0), build(d, spot, r, q, model, flow_w)
    rows = []
    for e in list(dm.sort_values("exp_ts")["expiry"].unique())[:n_exp]:
        ga, gm = da[da["expiry"] == e], dm[dm["expiry"] == e]
        if len(ga) < 10:
            continue
        tot = max(float(ga["gex"].sum()), 1e-9)
        call_g, put_g = ga[ga["side"] == 1], ga[ga["side"] == -1]
        up_zone = call_g[(call_g["strike"] >= spot) & (call_g["strike"] <= spot * 1.05)]["gex"].sum() / tot
        dn_zone = put_g[(put_g["strike"] <= spot) & (put_g["strike"] >= spot * 0.95)]["gex"].sum() / tot
        cw_s = call_g[(call_g["strike"] >= spot) & (call_g["strike"] <= spot * 1.15)].groupby("strike")["gex"].sum()
        pw_s = put_g[(put_g["strike"] <= spot) & (put_g["strike"] >= spot * 0.85)].groupby("strike")["gex"].sum()
        cw = float(cw_s.idxmax()) if len(cw_s) else np.nan
        pw = float(pw_s.idxmax()) if len(pw_s) else np.nan
        cdist = (cw / spot - 1) * 100 if cw == cw else np.nan
        pdist = (pw / spot - 1) * 100 if pw == pw else np.nan
        cvol, pvol = float(call_g["vol"].sum()), float(put_g["vol"].sum())
        coi, poi = float(call_g["oi"].sum()), float(put_g["oi"].sum())
        cshare = cvol / max(cvol + pvol, 1e-9)
        up = (35 * min(up_zone * 5, 1) + (25 * max(0, 1 - abs(cdist) / 8) if cdist == cdist else 0)
              + 20 * float(np.clip((cshare - 0.5) * 2, 0, 1)) + 20 * min(cvol / max(coi, 1e-9) / 0.5, 1))
        dn = (35 * min(dn_zone * 5, 1) + (25 * max(0, 1 - abs(pdist) / 8) if pdist == pdist else 0)
              + 20 * float(np.clip(((1 - cshare) - 0.5) * 2, 0, 1)) + 20 * min(pvol / max(poi, 1e-9) / 0.5, 1))
        xs, ys = exposure_profile(gm, spot, r, q, "gex", n=61)
        flip = find_flip(xs, ys, spot)
        sig = ("Upside squeeze watch" if up >= 60 and up >= dn else "Downside flush watch" if dn >= 60 else
               "Elevated" if max(up, dn) >= 40 else "Quiet")
        rows.append({"Symbol": sym.lstrip("^"), "Expiry": e, "DTE": round(float(ga["dte"].iloc[0]), 1), "Spot": round(spot, 2),
                     "Net GEX $M": round(float(gm["gex"].sum()), 1), "Flip": round(flip, 2) if flip else np.nan,
                     "Call wall": cw, "Call dist %": round(cdist, 1) if cdist == cdist else np.nan,
                     "Put wall": pw, "Put dist %": round(pdist, 1) if pdist == pdist else np.nan,
                     "C/P volume": round(cvol / max(pvol, 1e-9), 2), "Up score": round(up), "Down score": round(dn), "Signal": sig})
    return rows


# ============================================================ macro: calendar + news
KEY_EVENTS = re.compile(r"CPI|PPI|PCE|Non-Farm|NFP|FOMC|Federal Funds|Fed Chair|Powell|GDP|Unemployment|Retail Sales|ISM|JOLTS|Jobless|Rate Decision", re.I)


def next_event_chip():
    try:
        cal = load_calendar()
    except Exception:
        return ""
    if cal.empty:
        return ""
    now = time.time()
    nxt = cal[(cal["country"] == "USD") & (cal["impact"] == "High") & (cal["ts"] > now)].sort_values("ts")
    if nxt.empty:
        return ""
    e = nxt.iloc[0]
    secs = e["ts"] - now
    warn = " warn" if secs < 7200 else ""
    return f'<div class="chip{warn}"><b>{esc(str(e["title"]))}</b> <span>USD · high impact · in {cd(secs)}</span></div>'


def calendar_html(df, tz):
    p = pal()
    now = time.time()
    colors = {"High": p["dn"], "Medium": p["flip"], "Low": "#64748b"}
    rows = ""
    for _, e in df.iterrows():
        t = pd.Timestamp(e["ts"], unit="s", tz="UTC").tz_convert(tz)
        key = bool(KEY_EVENTS.search(str(e["title"])))
        past = e["ts"] < now
        cls = ("key " if key else "") + ("past" if past else "")
        tag = '<span class="keytag">KEY</span>' if key else ""
        when = "released" if past else cd(e["ts"] - now)
        rows += (f'<tr class="{cls}"><td>{t.strftime("%a %d %b %H:%M")}</td><td>{esc(str(e["country"]))}</td>'
                 f'<td>{esc(str(e["title"]))}{tag}</td><td><span class="imp" style="background:{colors.get(e["impact"], "#64748b")}"></span>{esc(str(e["impact"]))}</td>'
                 f'<td>{esc(str(e["forecast"]))}</td><td>{esc(str(e["previous"]))}</td><td>{when}</td></tr>')
    head = "<tr><th>Time</th><th>Cty</th><th>Event</th><th>Impact</th><th>Forecast</th><th>Previous</th><th>In</th></tr>"
    return f'<table class="cal">{head}{rows}</table>'


def news_html(items):
    out = ""
    for it in items:
        out += (f'<div class="item"><a href="{esc(it["link"])}" target="_blank" rel="noopener">{esc(it["title"])}</a>'
                f'<div class="meta">{esc(it["source"])} · {ago(it["ts"]) if it["ts"] else ""}</div></div>')
    return f'<div class="news">{out}</div>'


def news_page():
    sym = cur_sym()
    meta = meta_of(sym)
    st.markdown('<div class="panel-title">Economic calendar · CPI, FOMC, jobs and more</div>', unsafe_allow_html=True)
    cal = load_calendar()
    if cal.empty:
        st.info("Calendar feed unavailable right now (it may be rate limited). Try again in a few minutes.")
    else:
        a, b, c3 = st.columns([1.4, 1.4, 1])
        with a:
            st.multiselect("Impact", ["High", "Medium", "Low"], key="cal_impact")
        with b:
            countries = sorted(cal["country"].dropna().unique())
            S["cal_country"] = [x for x in S["cal_country"] if x in countries]
            st.multiselect("Countries", countries, key="cal_country")
        with c3:
            span = st.selectbox("Range", ["Upcoming", "Today", "All loaded"], key="cal_span")
        df = cal[cal["impact"].isin(S["cal_impact"])]
        if S["cal_country"]:
            df = df[df["country"].isin(S["cal_country"])]
        now = time.time()
        if span == "Upcoming":
            df = df[df["ts"] >= now - 7200]
        elif span == "Today":
            today = pd.Timestamp.now(tz=S["tz"]).normalize()
            lo, hi = today.timestamp(), (today + pd.Timedelta(days=1)).timestamp()
            df = df[(df["ts"] >= lo) & (df["ts"] < hi)]
        df = df.sort_values("ts").head(80)
        if df.empty:
            st.caption("No events match these filters.")
        else:
            st.markdown(calendar_html(df, S["tz"]), unsafe_allow_html=True)
        st.caption(f"Times shown in {S['tz']} (change in Display). Source: Forex Factory weekly feed. KEY marks CPI, FOMC, NFP and similar.")
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown(f'<div class="panel-title">Headlines · {esc(meta["name"])}</div>', unsafe_allow_html=True)
        try:
            st.markdown(news_html(load_headlines(meta["news"] + " when:3d")), unsafe_allow_html=True)
        except Exception:
            st.caption("Headlines unavailable right now.")
    with b:
        st.markdown('<div class="panel-title">Headlines · macro (CPI, Fed, jobs)</div>', unsafe_allow_html=True)
        try:
            st.markdown(news_html(load_headlines("(CPI OR inflation OR Fed OR FOMC OR jobs report OR Treasury yields) when:2d")),
                        unsafe_allow_html=True)
        except Exception:
            st.caption("Headlines unavailable right now.")


# ============================================================ options views
def header(c):
    if S["show_kpis"]:
        px = lambda v: fmt_k(v * c.ratio)
        dist = lambda x: f"{(x / c.spot - 1) * 100:+.2f}% from spot"
        if S["model"] == "Absolute":
            regime, rt = "Absolute", "amb"
        elif c.flip:
            regime, rt = ("Long gamma", "pos") if c.spot > c.flip else ("Short gamma", "neg")
        else:
            regime, rt = ("Long gamma", "pos") if c.net > 0 else ("Short gamma", "neg")
        nxt = c.exps[0]
        cards = [
            kpi(c.unit, px(c.spot), c.meta["name"]),
            kpi("Regime", regime, "vs gamma flip", rt),
            kpi("Net GEX", fmt_m(c.net), "$ per 1% move", "pos" if c.net >= 0 else "neg"),
            kpi("Gamma flip", px(c.flip) if c.flip else "n/a", dist(c.flip) if c.flip else "", "amb"),
            kpi("Call wall", px(c.call_wall) if c.call_wall else "n/a", dist(c.call_wall) if c.call_wall else "", "pos"),
            kpi("Put wall", px(c.put_wall) if c.put_wall else "n/a", dist(c.put_wall) if c.put_wall else "", "neg"),
            kpi("Max pain", px(c.pain), dist(c.pain)),
            kpi("Next expiry", cd(c.tsmap[nxt] - time.time()), f"{nxt} · {c.exps and exp_kind(c.tsmap[nxt])}", "acc"),
        ]
        st.markdown(f'<div class="kpi-grid">{"".join(cards)}</div>', unsafe_allow_html=True)
    if S["show_chips"]:
        chips = next_event_chip()
        for e in c.exps[:7]:
            on = " on" if e in c.sel else ""
            chips += f'<div class="chip{on}"><b>{e[5:]}</b> <span>{exp_kind(c.tsmap[e])} · {cd(c.tsmap[e] - time.time())}</span></div>'
        st.markdown(f'<div class="chips">{chips}</div>', unsafe_allow_html=True)
    if c.meta["group"] != "Crypto" and c.quote_share < 0.2:
        st.caption("Few live quotes (market closed or delayed): the data feed's IV is used, so levels are approximate.")


def view_options(c):
    main, side = st.columns([3.3, 1.3], gap="medium")
    with main:
        metric = pills("opt_metric", list(METRICS))
        show(bars_fig(c, metric, S["h_main"]), "opt_main")
        panels = S["c_panels"]
        if panels:
            cols = st.columns(len(panels), gap="small")
            for i, (col, name) in enumerate(zip(cols, panels)):
                with col:
                    st.markdown(f'<div class="panel-title">{name}</div>', unsafe_allow_html=True)
                    show(bars_fig(c, name, 280), f"opt_p{i}")
    with side:
        st.markdown('<div class="panel-title">Major walls</div>', unsafe_allow_html=True)
        wm = pills("wall_metric", ["GEX", "DEX", "CHEX", "VEX"])
        st.markdown(walls_html(c, METRICS[wm]), unsafe_allow_html=True)
        st.markdown(brief_html(c), unsafe_allow_html=True)


def view_expiry(c):
    metric = pills("exp_metric", ["GEX", "VEX", "DEX", "CHEX", "OI", "Volume"])
    col = METRICS[metric]
    t = expiry_stats(c, col)
    fmtv = (lambda v: f"{v:,.0f}") if col in ("oi", "vol") else fmt_m
    k1, k2, k3 = st.columns(3)
    k1.markdown(kpi("Total calls", fmtv(float(t["call"].sum())), "all loaded expirations", "pos"), unsafe_allow_html=True)
    k2.markdown(kpi("Total puts", fmtv(float(t["put"].sum())), "all loaded expirations", "neg"), unsafe_allow_html=True)
    k3.markdown(kpi("Net", fmtv(float(t["net"].sum())), "calls and puts combined", "pos" if t["net"].sum() >= 0 else "neg"),
                unsafe_allow_html=True)
    st.markdown('<div class="panel-title">Exposure by expiry (full brightness = included in levels)</div>', unsafe_allow_html=True)
    show(expiry_fig(c, metric, t, 400), "exp_chart")
    tab = t[["expiry", "kind", "dte", "call", "put", "net", "share", "call_oi", "put_oi", "call_vol", "put_vol"]].copy()
    tab.columns = ["Expiry", "Type", "DTE", f"Call {metric}", f"Put {metric}", "Net", "Share of |net| %",
                   "Call OI", "Put OI", "Call vol", "Put vol"]
    st.dataframe(tab.round(2), hide_index=True)
    if st.toggle("Show strike × expiry heatmap", key="show_heat"):
        hm = pills("hm_metric", ["GEX", "DEX", "VEX", "CHEX", "OI", "Volume", "IV"])
        show(heat_fig(c, hm, 480), "heatmap")


def view_profiles(c):
    metric = pills("prof_metric", ["GEX", "VEX", "DEX", "CHEX"])
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown(f'<div class="panel-title">{metric} price profile · total exposure if spot moves</div>', unsafe_allow_html=True)
        show(profile_fig(c, metric, 360), "pf_price")
    with b:
        st.markdown('<div class="panel-title">OI strike profile</div>', unsafe_allow_html=True)
        show(oi_profile_fig(c, 360), "pf_oi")
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown('<div class="panel-title">OI and volume profile (with volume / OI)</div>', unsafe_allow_html=True)
        show(oivol_fig(c, 440), "pf_oivol")
    with b:
        st.markdown(f'<div class="panel-title">Cumulative {metric} profile across strikes</div>', unsafe_allow_html=True)
        show(cum_fig(c, metric, 440), "pf_cum")


def view_ladder(c):
    a, b = st.columns([1.5, 1], gap="large")
    with a:
        metric = pills("lad_metric", ["GEX", "DEX", "VEX", "CHEX", "OI", "Volume"])
        st.markdown(ladder_html(c, METRICS[metric]), unsafe_allow_html=True)
    with b:
        st.markdown('<div class="panel-title">Key levels</div>', unsafe_allow_html=True)
        t = levels_table(c)
        st.dataframe(t.round(2), hide_index=True)
        st.download_button("Download levels (CSV)", t.to_csv(index=False), file_name=f"levels_{c.sym.lstrip('^')}.csv", mime="text/csv")


def view_screener(c):
    st.markdown('<div class="panel-title">Gamma squeeze screener · scans on demand to keep the app fast</div>', unsafe_allow_html=True)
    a, b, c3 = st.columns([3, 1, 1])
    with a:
        symbols = st.multiselect("Markets to scan", list(MARKETS), default=[s for s in S["c_watch"] if s in MARKETS][:6],
                                 key="scan_syms", format_func=lambda s: s.lstrip("^"))
    with b:
        n = st.slider("Expiries each", 1, 4, 2, key="scan_n")
    with c3:
        st.write("")
        go_scan = st.button("Run scan", type="primary", key="scan_go")
    if go_scan and symbols:
        rows, bar = [], st.progress(0.0, text="Scanning...")
        for i, s in enumerate(symbols):
            try:
                rows += analyze_symbol(s, n, S["model"], S["flow_w"], S["rate"] / 100)
            except Exception:
                pass
            bar.progress((i + 1) / len(symbols), text=f"Scanned {s.lstrip('^')}")
        bar.empty()
        t = pd.DataFrame(rows)
        if not t.empty:
            t = t.reindex(t[["Up score", "Down score"]].max(axis=1).sort_values(ascending=False).index).reset_index(drop=True)
        S["scan"], S["scan_t"] = t, time.time()
    t = S.get("scan")
    if t is None:
        st.info("Pick markets and press Run scan. Each new market takes a few seconds to download.")
        return
    if t.empty:
        st.warning("No data returned. Try fewer markets or check your connection.")
        return
    try:
        cfg = {"Up score": st.column_config.ProgressColumn("Up score", min_value=0, max_value=100, format="%d"),
               "Down score": st.column_config.ProgressColumn("Down score", min_value=0, max_value=100, format="%d")}
    except Exception:
        cfg = None
    st.dataframe(t, hide_index=True, column_config=cfg)
    st.caption(f"Scanned {ago(S['scan_t'])}. Scores (0-100) combine call (or put) gamma within 5% of spot, wall proximity, "
               "call/put volume skew and volume turnover versus open interest. A heuristic for pressure near spot, not a prediction.")


def view_surfaces(c):
    kind = pills("surf_kind", ["IV", "GEX", "VEX", "DEX", "Charm"])
    f = surface_fig(c, kind, 650)
    if f is None:
        st.info("Not enough expirations to draw the surface. Increase 'Expirations to load' in the sidebar.")
    else:
        show(f, "surface")
    st.caption("Charm surface shows how dealer delta exposure decays per day across strike and expiry.")


def view_data(c):
    sub = c.d_sel[["expiry", "strike", "side", "oi", "vol", "iv_s", "pos", "gex", "dex", "vex", "chex"]].copy()
    sub["side"] = np.where(sub["side"] == 1, "Call", "Put")
    st.dataframe(sub.round(4).head(150), hide_index=True)
    st.download_button("Download selected-expiry data (CSV)", sub.to_csv(index=False),
                       file_name=f"options_{c.sym.lstrip('^')}.csv", mime="text/csv")
    st.caption(f"Quote-implied IV: {c.quote_share * 100:.0f}% of contracts. Others use the data feed's IV.")
    with st.expander("How it works", expanded=False):
        st.markdown(
            """
**Data.** Stocks, ETFs and indices come from Yahoo Finance (delayed). BTC and ETH come from Deribit. Chains, vols and
calculations are cached, and only the view you open is drawn, which keeps the app responsive.

**Implied volatility.** Where a live bid/ask exists, IV is re-computed from the mid price with Black-Scholes (including
dividend yield). Otherwise the feed's IV is used. With smoothing on, a robust polynomial is fitted per expiry and outliers
are rejected.

**Exposures.** GEX is dollar gamma per 1% move, DEX dollar delta, VEX dollar vanna per vol point, CHEX charm (delta decay per day).

**Positioning models.** Standard: dealers long calls, short puts. Crypto retail: the reverse. Flow-adjusted: Standard plus an
estimate of today's customer flow. Absolute: unsigned gamma. No retail source shows true dealer positioning, so all
figures are estimates.

**Levels.** Gamma flip is where total GEX crosses zero as spot moves. Call wall is the largest call GEX at or above spot,
put wall the largest put GEX at or below. Expected move is ATM IV x spot x sqrt(time).

**Expiries roll live.** Auto modes drop contracts as they expire (4pm ET for US options, 08:00 UTC for Deribit) and pick up
the next ones. Turn on Live in the top bar for periodic refresh.
            """
        )


OPTION_VIEWS = {"Options": view_options, "GEX by Expiry": view_expiry, "Profiles": view_profiles,
                "Ladder & Levels": view_ladder, "Squeeze Screener": view_screener, "Surfaces": view_surfaces,
                "Data & Model": view_data}


def options_page():
    try:
        c = get_ctx()
    except Exception as e:
        st.error(f"Could not load options for {cur_sym()}: {e}")
        return
    if c is None:
        st.warning("No usable option data right now (market closed, or this symbol has no options).")
        return
    header(c)
    OPTION_VIEWS[S["nav"]](c)


# ============================================================ replay + paper trading
def sim_equity(px):
    s = S["sim"]
    return s["bal"] + (s["pos"] * (px - s["entry"]) if s["pos"] else 0.0)


def sim_close(px, lab, why):
    s = S["sim"]
    if not s["pos"]:
        return
    pnl = s["pos"] * (px - s["entry"])
    fee = abs(s["pos"]) * px * s["fee"]
    s["bal"] += pnl - fee
    s["log"].append(dict(time=str(lab), side="Long" if s["pos"] > 0 else "Short", qty=round(abs(s["pos"]), 6),
                         entry=round(s["entry"], 4), exit=round(px, 4), pnl=round(pnl - fee, 2), reason=why))
    s["marks"].append(dict(lab=lab, px=px, side=-1 if s["pos"] > 0 else 1))
    s["pos"], s["entry"], s["sl"], s["tp"] = 0.0, 0.0, None, None


def sim_check(hi, lo, lab):
    s = S["sim"]
    if not s["pos"]:
        return
    if s["pos"] > 0:
        if s["sl"] and lo <= s["sl"]:
            sim_close(s["sl"], lab, "stop")
        elif s["tp"] and hi >= s["tp"]:
            sim_close(s["tp"], lab, "target")
    else:
        if s["sl"] and hi >= s["sl"]:
            sim_close(s["sl"], lab, "stop")
        elif s["tp"] and lo <= s["tp"]:
            sim_close(s["tp"], lab, "target")


def sim_open(side):
    s = S["sim"]
    px, lab = S["sim_px"], S["sim_t"]
    if not px:
        return
    if s["pos"] and np.sign(s["pos"]) != side:
        sim_close(px, lab, "reverse")
    if s["pos"]:
        return
    qty = S["sim_usd"] / px * side
    s["bal"] -= abs(qty) * px * s["fee"]
    s["pos"], s["entry"] = qty, px
    s["sl"] = px * (1 - side * S["sim_sl"] / 100) if S["sim_sl"] > 0 else None
    s["tp"] = px * (1 + side * S["sim_tp"] / 100) if S["sim_tp"] > 0 else None
    s["marks"].append(dict(lab=lab, px=px, side=side))


def sim_flat():
    sim_close(S["sim_px"], S["sim_t"], "manual")


def sim_reset():
    S["sim"] = new_sim()


def rp_step(d):
    S["rp_i"] = max(30, S["rp_i"] + d)
    S["rp_play"] = False


def rp_toggle():
    S["rp_play"] = not S["rp_play"]


def replay_controls():
    c1, c2, c3, c4, c5, c6, c7 = st.columns([1.5, 0.8, 0.9, 0.8, 1.2, 1, 1.1])
    with c1:
        pills("sim_mode", ["Replay", "Live paper"])
    if S["sim_mode"] == "Replay":
        with c2:
            st.button("◀ Back", on_click=rp_step, args=(-1,), key="rp_back")
        with c3:
            st.button("⏸ Pause" if S["rp_play"] else "▶ Play", on_click=rp_toggle, key="rp_playbtn", type="primary")
        with c4:
            st.button("Next ▶", on_click=rp_step, args=(1,), key="rp_next")
        with c5:
            st.slider("Speed (candles/tick)", 1, 8, key="rp_speed")
        with c6:
            st.selectbox("Timeframe", list(TF), key="rp_tf")
    with c7:
        st.checkbox("Show GEX levels", key="rp_levels")


def candle_fig(k, h, levels=(), marks=(), simlines=(), ratio=1.0):
    p = pal()
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.8, 0.2], vertical_spacing=0.02)
    fig.add_trace(go.Candlestick(
        x=k["lab"], open=k["o"] * ratio, high=k["h"] * ratio, low=k["l"] * ratio, close=k["c"] * ratio, name="Price",
        increasing=dict(line=dict(color=p["up"]), fillcolor=p["up"]),
        decreasing=dict(line=dict(color=p["dn"]), fillcolor=p["dn"])), row=1, col=1)
    fig.add_trace(go.Bar(x=k["lab"], y=k["v"], name="Volume", showlegend=False,
                         marker_color=[p["up"] if c_ >= o_ else p["dn"] for c_, o_ in zip(k["c"], k["o"])], opacity=0.6), row=2, col=1)
    lo, hi = float(k["l"].min() * ratio), float(k["h"].max() * ratio)
    pad = (hi - lo) * 0.3
    for v, label, color in levels:
        if lo - pad <= v <= hi + pad:
            fig.add_hline(y=v, line_dash="dash", line_color=color, line_width=1, annotation_text=label,
                          annotation_font_color=color, annotation_position="top right", row=1, col=1)
    for v, label, color in simlines:
        fig.add_hline(y=v, line_color=color, line_width=1.5, annotation_text=label, annotation_font_color=color,
                      annotation_position="bottom right", row=1, col=1)
    labs = set(k["lab"])
    for m in marks:
        if m["lab"] in labs:
            fig.add_trace(go.Scatter(x=[m["lab"]], y=[m["px"]], mode="markers", showlegend=False,
                                     marker=dict(symbol="triangle-up" if m["side"] > 0 else "triangle-down", size=13,
                                                 color="#ffffff", line=dict(width=1, color="#000000"))), row=1, col=1)
    style(fig, h, showlegend=False)
    fig.update_xaxes(type="category", nticks=8, rangeslider_visible=False)
    return fig


def sim_panel(px):
    s = S["sim"]
    eq = sim_equity(px)
    unreal = s["pos"] * (px - s["entry"]) if s["pos"] else 0.0
    wins = sum(1 for x in s["log"] if x["pnl"] > 0)
    pos_txt = (("LONG " if s["pos"] > 0 else "SHORT ") + f"{abs(s['pos']):.4f}") if s["pos"] else "FLAT"
    cards = [
        kpi("Equity", f"${eq:,.2f}", f"start $10,000 · {(eq / 10000 - 1) * 100:+.2f}%", "pos" if eq >= 10000 else "neg"),
        kpi("Position", pos_txt, f"entry {fmt_px(s['entry'])}" if s["pos"] else "no open position", "acc"),
        kpi("Unrealized", f"${unreal:+,.2f}", "mark to market", "pos" if unreal >= 0 else "neg"),
        kpi("Trades", f"{len(s['log'])}", f"win rate {wins / len(s['log']) * 100:.0f}%" if s["log"] else "no closed trades"),
    ]
    st.markdown(f'<div class="kpi-grid">{"".join(cards)}</div>', unsafe_allow_html=True)
    st.number_input("Size ($)", min_value=10.0, step=100.0, key="sim_usd")
    a, b = st.columns(2)
    with a:
        st.number_input("Stop %", min_value=0.0, step=0.25, key="sim_sl")
    with b:
        st.number_input("Target %", min_value=0.0, step=0.25, key="sim_tp")
    a, b, c3 = st.columns(3)
    with a:
        st.button("Buy", on_click=sim_open, args=(1,), key="sim_buy", type="primary")
    with b:
        st.button("Sell", on_click=sim_open, args=(-1,), key="sim_sell")
    with c3:
        st.button("Close", on_click=sim_flat, key="sim_close")
    st.button("Reset account", on_click=sim_reset, key="sim_reset")
    if s["log"]:
        st.dataframe(pd.DataFrame(s["log"][::-1]), hide_index=True)
    st.caption("Paper trading only: market fills at the shown price, 0.05% fee per side, no leverage or liquidation.")


def replay_levels(ratio):
    if not S["rp_levels"]:
        return []
    try:
        c = get_ctx()
    except Exception:
        return []
    if c is None:
        return []
    p = pal()
    return [(v * ratio, label, color) for v, label, color in
            ((c.flip, "Flip", p["flip"]), (c.call_wall, "Call wall", p["up"]), (c.put_wall, "Put wall", p["dn"]),
             (c.pain, "Max pain", p["acc"])) if v]


def replay_body():
    sym = cur_sym()
    csym = meta_of(sym).get("candle") or sym
    s = S["sim"]
    tf = S["rp_tf"] if S["sim_mode"] == "Replay" else "1m"
    try:
        k = load_candles(csym, tf)
    except Exception as e:
        st.error(f"Could not load candles for {csym}: {e}")
        return
    if k.empty or len(k) < 80:
        st.warning("Not enough price history for this market and timeframe.")
        return
    n = len(k)
    ratio = get_ratio(float(k["c"].iloc[-1]), sym)
    if S["sim_mode"] == "Replay":
        key = (sym, tf)
        if S["rp_key"] != key or S["rp_i"] >= n or S["rp_i"] < 30:
            S["rp_key"] = key
            S["rp_i"] = max(40, n - 200) if n > 260 else max(30, n // 2)
            S["rp_prev"] = S["rp_i"]
        if S["rp_play"]:
            S["rp_i"] = min(S["rp_i"] + S["rp_speed"], n - 1)
            if S["rp_i"] >= n - 1:
                S["rp_play"] = False
        i = st.slider("Replay position (candle)", 30, n - 1, key="rp_i")
        if i > S["rp_prev"]:
            for j in range(S["rp_prev"] + 1, i + 1):
                sim_check(float(k["h"].iloc[j] * ratio), float(k["l"].iloc[j] * ratio), k["lab"].iloc[j])
        S["rp_prev"] = i
        S["sim_px"], S["sim_t"] = float(k["c"].iloc[i] * ratio), k["lab"].iloc[i]
        view = k.iloc[max(0, i - 119): i + 1]
        caption = f"Replaying {csym} {tf} · candle {k['t'].iloc[i]} UTC · GEX levels are current, not historical"
    else:
        view = k.tail(120)
        px = float(view["c"].iloc[-1] * ratio)
        S["sim_px"], S["sim_t"] = px, view["lab"].iloc[-1]
        sim_check(px, px, S["sim_t"])
        caption = f"Live paper trading on {csym} · last price {fmt_px(px)} (delayed feed)"
    p = pal()
    simlines = []
    if s["pos"]:
        simlines.append((s["entry"], "Entry", "#ffffff"))
        if s["sl"]:
            simlines.append((s["sl"], "Stop", p["dn"]))
        if s["tp"]:
            simlines.append((s["tp"], "Target", p["up"]))
    st.caption(caption)
    a, b = st.columns([2.6, 1.2], gap="large")
    with a:
        show(candle_fig(view, 620, replay_levels(ratio), s["marks"], simlines, ratio), "replay_chart")
    with b:
        sim_panel(S["sim_px"])


# ============================================================ top bar
def us_status():
    t = pd.Timestamp.now(tz="America/New_York")
    open_ = t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)
    return ("US MARKET OPEN" if open_ else "US MARKET CLOSED"), t.strftime("%H:%M ET")


def tape_html():
    try:
        quotes = fetch_quotes(tuple(S["c_watch"]))
    except Exception:
        quotes = []
    items = ""
    for s, last, chg in quotes:
        if last is None:
            continue
        tone = "pos" if chg >= 0 else "neg"
        items += f'<span><b>{s.lstrip("^")}</b>{fmt_px(last)} <span class="{tone}">{chg:+.2f}%</span></span>'
    status, tm = us_status()
    return f'<div class="tapebar">{items}<span class="st">{status} · {tm}</span></div>'


wl = S["c_watch"]
top = st.columns([1.7] + [0.75] * len(wl) + [0.7, 1.15])
with top[0]:
    st.markdown('<div class="brand">Market<i>.</i>Terminal</div>', unsafe_allow_html=True)
for col_, s in zip(top[1:-2], wl):
    with col_:
        try:
            st.button(s.lstrip("^"), key=f"wl_{s}", on_click=set_sym, args=(s,), width="stretch",
                      type="primary" if (s == S["market_sym"] and not S["custom_sym"].strip()) else "secondary")
        except TypeError:
            st.button(s.lstrip("^"), key=f"wl_{s}", on_click=set_sym, args=(s,), use_container_width=True,
                      type="primary" if (s == S["market_sym"] and not S["custom_sym"].strip()) else "secondary")
with top[-2]:
    st.toggle("Live", key="live_on")
with top[-1]:
    box = st.popover("🎨 Customize") if hasattr(st, "popover") else st.expander("🎨 Customize")
    with box:
        st.selectbox("Theme preset", list(THEMES), key="theme_pick", on_change=apply_theme)
        c1, c2 = st.columns(2)
        with c1:
            st.color_picker("Up / positive", key="col_up")
            st.color_picker("Accent", key="col_accent")
            st.color_picker("Background", key="col_bg")
        with c2:
            st.color_picker("Down / negative", key="col_down")
            st.color_picker("Levels / flip", key="col_flip")
            st.color_picker("Panels", key="col_panel")
        st.multiselect("Quick-switch markets", list(MARKETS), key="c_watch", max_selections=9, format_func=lambda s: s.lstrip("^"))
        st.caption("Colors apply to every chart and the interface. Saved automatically.")
st.markdown(tape_html(), unsafe_allow_html=True)

# ============================================================ sidebar
sb = st.sidebar
sb.markdown('<div class="sbbrand">Controls</div>', unsafe_allow_html=True)
sb.markdown(
    f'<div class="livepill"><span class="dot {"" if S["live_on"] else "off"}"></span>'
    f'{"LIVE · every " + str(S["live_sec"]) + "s" if S["live_on"] else "PAUSED · manual refresh"}</div>', unsafe_allow_html=True)
ex_mk = sb.expander("Market", expanded=True)
ex_exp = sb.expander("Expiries & refresh", expanded=True)
ex_opt = sb.expander("Options model", expanded=False)
ex_cfd = sb.expander("CFD mapping", expanded=False)
ex_disp = sb.expander("Display", expanded=False)

with ex_mk:
    st.selectbox("Market", list(MARKETS), key="market_sym",
                 format_func=lambda s: f"{s.lstrip('^')} · {MARKETS[s]['name']} ({MARKETS[s]['group']})")
    st.text_input("Or any optionable symbol", key="custom_sym", placeholder="e.g. COIN, XLK, MSTR")
    st.slider("Expirations to load", 1, 10, key="n_exp", help="More expirations = slower first load")
    st.slider("Strike window (± % of spot)", 3, 30, key="win_pct", help="Crypto uses 2x this value")
    st.slider("Max strikes shown", 15, 71, step=2, key="max_strikes")

sym = cur_sym()
meta = meta_of(sym)

with ex_exp:
    st.selectbox("Expiry selection", EXP_MODES, key="exp_mode")
    if S["exp_mode"] == "Auto: next N expiries":
        st.slider("Number of expiries", 1, 10, key="exp_n")
    elif S["exp_mode"] == "Auto: within N days":
        st.slider("Days ahead", 1, 90, key="exp_days")
    else:
        try:
            _, _raw = load_chain(sym, S["n_exp"])
            all_exps = list(_raw[_raw["exp_ts"] > time.time()].sort_values("exp_ts")["expiry"].unique())
            S["exp_manual"] = [e for e in S["exp_manual"] if e in all_exps]
            st.multiselect("Expiries", all_exps, key="exp_manual")
        except Exception:
            st.caption("Could not load expiries.")
    st.slider("Live refresh (seconds)", 15, 300, step=15, key="live_sec")
    st.caption("Auto modes roll forward by themselves when a contract expires. Turn Live on in the top bar to refresh on a timer.")

with ex_opt:
    st.selectbox("Dealer positioning", list(MODELS), key="model")
    st.caption(MODELS[S["model"]])
    if S["model"] == "Flow-adjusted":
        st.slider("Flow weight", 0.0, 1.0, step=0.05, key="flow_w")
    st.checkbox("Smooth IV (reject bad quotes)", key="smooth")
    st.number_input("Risk-free rate (%)", step=0.25, format="%.2f", key="rate")
    st.number_input("Dividend yield (%)", value=float(meta["div"]), step=0.1, key=f"q_{sym}")

with ex_cfd:
    st.text_input("CFD symbol (MT5 name)", meta.get("cfd") or sym.lstrip("^"), key=f"cfd_{sym}")
    st.checkbox("Pull CFD price from MT5", key="use_mt5")
    st.number_input("CFD price (manual, 0 = off)", min_value=0.0, value=0.0, key=f"cp_{sym}")
    st.caption("Enter your broker's price to convert every level to CFD terms.")

with ex_disp:
    st.slider("Main chart height", 360, 800, step=20, key="h_main")
    st.radio("Bar orientation", ["Horizontal", "Vertical"], horizontal=True, key="orient")
    st.multiselect("Chart levels", LEVEL_OPTIONS, key="c_levels")
    st.multiselect("Extra panels under main chart (max 3)", PANEL_OPTIONS, key="c_panels", max_selections=3)
    st.selectbox("Time zone", TZS, key="tz")
    st.checkbox("Show KPI cards", key="show_kpis")
    st.checkbox("Show expiry and event chips", key="show_chips")

# ============================================================ router
nav = pills("nav", NAVS)
every = float(S["live_sec"]) if S["live_on"] else None
if nav == "News & Calendar":
    run_fragment(news_page, every)
elif nav == "Replay & Sim":
    replay_controls()
    run_fragment(replay_body, 1.0 if (S["rp_play"] and S["sim_mode"] == "Replay") else every)
else:
    run_fragment(options_page, every)

save_settings()
