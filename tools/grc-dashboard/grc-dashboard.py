#!/usr/bin/env python3
"""
Gregcoin Mining Dashboard — htop-style TUI
  Tab / F1-F3 to switch views   Shift+Tab: previous
  [s]tart  [x]stop  [r]estart  [1]25%  [2]50%  [3]100%  [q]uit
  Transactions view: ↑↓ / PgUp / PgDn to scroll
"""
import base64
import collections
import curses
import json
import socket
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────────
def _load_rpc_config():
    """Read RPC credentials from ~/.gregcoin/gregcoin.conf at startup."""
    conf = {}
    try:
        path = os.path.expanduser("~/.gregcoin/gregcoin.conf")
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    conf[k.strip()] = v.strip()
    except Exception:
        pass
    return conf

import os as _os
_rpc_conf = _load_rpc_config()

RPC_URL        = f"http://127.0.0.1:{_rpc_conf.get('rpcport', '8445')}"
RPC_USER       = _rpc_conf.get("rpcuser", "grcuser")
RPC_PASS       = _rpc_conf.get("rpcpassword", "")
POOL_STATS_URL = "http://127.0.0.1:3334"
WALLET_ADDR    = "grc1qthh3zwq09k22yqegv7265xgfvzx447y3rwf3a0"
MINER_CTL      = "/home/pi/gregcoin/tools/miner-control.sh"
MINER_API_PORT = 4048
REFRESH_S      = 3
BLOCK_TIME_S   = 150
MAX_BLOCKS     = 8
KHS_HISTORY    = 80   # data points ≈ 4 minutes at 3 s/refresh

# Static fallback — used if kubectl is unavailable
STATIC_NODES = [
    ("picard",  "127.0.0.1"),
    ("riker",   "10.0.1.221"),
    ("data",    "10.0.1.222"),
    ("laforge", "10.0.1.223"),
    ("worf",    "10.0.1.224"),
    ("lore",    "10.0.1.218"),
    ("troi",    "10.0.1.219"),
]

NODE_REDISCOVER_S = 60   # re-query kubectl every N seconds

VIEWS = ["Overview", "Transactions", "Mining Stats", "Pool Miners"]

# ── Color pair IDs ─────────────────────────────────────────────────────────────
C_HEADER   = 1
C_MINING   = 2
C_STOPPED  = 3
C_OFFLINE  = 4
C_DIM      = 5
C_BOLD     = 6
C_BAR_F    = 7
C_BAR_E    = 8
C_LABEL    = 9
C_KEYS     = 10
C_FLASH    = 11
C_WARN     = 12
C_TAB_ACT  = 13
C_TAB_INA  = 14
C_ACCENT   = 15

# ── Data structures ────────────────────────────────────────────────────────────
@dataclass
class NodeStats:
    name: str
    api_ip: str
    khs: float = 0.0
    accepted: int = 0
    rejected: int = 0
    status: str = "OFFLINE"

@dataclass
class ChainInfo:
    blocks: int = 0
    difficulty: float = 0.0
    net_khs: float = 0.0
    balance: float = 0.0
    last_block_time: int = 0
    last_block_hash: str = ""
    recent_blocks: list = field(default_factory=list)
    error: str = ""

@dataclass
class TxOut:
    address: str
    value: float

@dataclass
class TxInfo:
    txid: str
    block_height: int
    block_time: int
    is_coinbase: bool
    total_out: float
    outputs: list   # list[TxOut]

# ── RPC helpers ────────────────────────────────────────────────────────────────
def rpc(method: str, params=None) -> Optional[dict]:
    payload = json.dumps({"method": method, "params": params or [], "id": 1}).encode()
    creds = base64.b64encode(f"{RPC_USER}:{RPC_PASS}".encode()).decode()
    req = urllib.request.Request(RPC_URL, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {creds}",
    })
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())["result"]
    except Exception:
        return None

def fetch_chain() -> ChainInfo:
    ci = ChainInfo()
    try:
        info = rpc("getblockchaininfo")
        if not info:
            ci.error = "RPC unavailable"
            return ci
        ci.blocks     = info.get("blocks", 0)
        ci.difficulty = info.get("difficulty", 0.0)
        mining = rpc("getmininginfo") or {}
        ci.net_khs = mining.get("networkhashps", 0) / 1000.0
        ci.balance = rpc("getbalance") or 0.0
        recent = []
        start = max(1, ci.blocks - MAX_BLOCKS + 1)
        for height in range(start, ci.blocks + 1):
            bh = rpc("getblockhash", [height])
            if bh:
                blk = rpc("getblock", [bh])
                if blk:
                    recent.append({
                        "height": blk["height"],
                        "time":   blk["time"],
                        "hash":   blk["hash"],
                        "diff":   blk.get("difficulty", 0),
                        "ntx":    len(blk.get("tx", [])),
                    })
        if recent:
            ci.last_block_time = recent[-1]["time"]
            ci.last_block_hash = recent[-1]["hash"]
        ci.recent_blocks = list(reversed(recent))
    except Exception as e:
        ci.error = str(e)
    return ci

def fetch_transactions(ci: ChainInfo) -> list:
    """Return TxInfo list from recent blocks (uses getblock verbosity=2)."""
    txs = []
    for meta in ci.recent_blocks[:MAX_BLOCKS]:
        blk = rpc("getblock", [meta["hash"], 2])
        if not blk:
            continue
        for tx in blk.get("tx", []):
            is_cb = any("coinbase" in vin for vin in tx.get("vin", []))
            outputs = []
            total_out = 0.0
            for vout in tx.get("vout", []):
                val = vout.get("value", 0.0)
                spk = vout.get("scriptPubKey", {})
                addr = spk.get("address") or (
                    spk["addresses"][0] if spk.get("addresses") else "(nonstandard)"
                )
                outputs.append(TxOut(address=addr, value=val))
                total_out += val
            txs.append(TxInfo(
                txid=tx["txid"],
                block_height=blk["height"],
                block_time=blk["time"],
                is_coinbase=is_cb,
                total_out=total_out,
                outputs=outputs,
            ))
    return txs

# ── Pool stats API ─────────────────────────────────────────────────────────────
def fetch_pool_miners() -> dict:
    """Fetch connected miner stats from grc-pool stats API."""
    try:
        req = urllib.request.Request(POOL_STATS_URL, headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return {"miners": [], "total_miners": 0, "total_hashrate_khs": 0.0,
                "pending_connections": 0, "error": "pool offline"}

# ── Miner API ──────────────────────────────────────────────────────────────────
def fetch_miner(ip: str) -> NodeStats:
    ns = NodeStats(name="", api_ip=ip)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((ip, MINER_API_PORT))
        s.sendall(b"summary\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"|" in chunk or b";" in chunk:
                break
        s.close()
        text = data.decode(errors="ignore")
        pairs = {}
        for sep in (";", "|"):
            if sep in text:
                for part in text.split(sep):
                    if "=" in part:
                        k, _, v = part.partition("=")
                        pairs[k.strip()] = v.strip()
                break
        if pairs:
            ns.khs      = float(pairs.get("KHS", pairs.get("KHS_30s", 0)) or 0)
            ns.accepted = int(pairs.get("ACC", 0) or 0)
            ns.rejected = int(pairs.get("REJ", 0) or 0)
            ns.status   = "MINING"
        else:
            ns.status = "STOPPED"
    except (ConnectionRefusedError, OSError):
        ns.status = "STOPPED"
    except Exception:
        ns.status = "OFFLINE"
    return ns

# ── Formatting helpers ─────────────────────────────────────────────────────────
def fmt_khs(khs: float) -> str:
    return f"{khs/1000:.2f} MH/s" if khs >= 1000 else f"{khs:.1f} KH/s"

def fmt_age(ts: int) -> str:
    if ts == 0:
        return "—"
    age = int(time.time()) - ts
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age//60}m {age%60:02d}s ago"
    return f"{age//3600}h {(age%3600)//60}m ago"

def fmt_eta(last_ts: int) -> str:
    if last_ts == 0:
        return "—"
    eta = max(0, BLOCK_TIME_S - (int(time.time()) - last_ts))
    return "any moment!" if eta == 0 else f"~{eta//60}m {eta%60:02d}s"

def shorten(s: str, pre: int = 10, suf: int = 10) -> str:
    return s[:pre] + "…" + s[-suf:] if len(s) > pre + suf + 1 else s

def progress_bar(last_ts: int, width: int) -> tuple:
    if last_ts == 0:
        return "░" * width, 0.0
    pct = min(1.0, (int(time.time()) - last_ts) / BLOCK_TIME_S)
    f = int(pct * width)
    return "█" * f + "░" * (width - f), pct

def share_bar(share: float, width: int) -> str:
    f = max(0, min(width, int(share * width)))
    return "█" * f + "░" * (width - f)

# Sparkline using unicode block elements (8 levels)
_SPARK = " ▁▂▃▄▅▆▇█"

def sparkline_row(values, width: int) -> str:
    if not values:
        return " " * width
    max_v = max(values) or 1.0
    pts = _resample(list(values), width)
    return "".join(_SPARK[min(8, int(v / max_v * 8))] for v in pts)

def bar_chart(values, width: int, height: int) -> list:
    """Multi-row bar chart. Returns list of `height` strings, each `width` wide."""
    if not values:
        return [" " * width] * height
    max_v = max(values) or 1.0
    pts = _resample(list(values), width)
    grid = [[" "] * width for _ in range(height)]
    for col, v in enumerate(pts):
        fill = int(v / max_v * height)
        for r in range(fill):
            grid[height - 1 - r][col] = "█"
    return ["".join(row) for row in grid]

def _resample(values: list, width: int) -> list:
    if len(values) <= width:
        return values + [0.0] * (width - len(values))
    step = len(values) / width
    return [values[int(i * step)] for i in range(width)]

def run_ctl(cmd: str, cpu_pct: int = 100):
    args = [MINER_CTL, cmd]
    if cmd in ("start", "restart"):
        args += ["--cpu", str(cpu_pct)]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def discover_nodes() -> list:
    """Return [(name, api_ip)] from kubectl node list, falling back to STATIC_NODES."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            found = []
            for item in data.get("items", []):
                name = item["metadata"]["name"]
                ip = None
                for addr in item.get("status", {}).get("addresses", []):
                    if addr["type"] == "InternalIP":
                        ip = addr["address"]
                        break
                if ip:
                    api_ip = "127.0.0.1" if name == "picard" else ip
                    found.append((name, api_ip))
            if found:
                return sorted(found, key=lambda x: x[0])
    except Exception:
        pass
    return list(STATIC_NODES)

# ── curses helpers ─────────────────────────────────────────────────────────────
def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER,  curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(C_MINING,  curses.COLOR_GREEN,   -1)
    curses.init_pair(C_STOPPED, curses.COLOR_RED,     -1)
    curses.init_pair(C_OFFLINE, curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_DIM,     curses.COLOR_WHITE,   -1)
    curses.init_pair(C_BOLD,    curses.COLOR_WHITE,   -1)
    curses.init_pair(C_BAR_F,   curses.COLOR_GREEN,   curses.COLOR_GREEN)
    curses.init_pair(C_BAR_E,   curses.COLOR_BLACK,   curses.COLOR_BLACK)
    curses.init_pair(C_LABEL,   curses.COLOR_CYAN,    -1)
    curses.init_pair(C_KEYS,    curses.COLOR_WHITE,   curses.COLOR_BLACK)
    curses.init_pair(C_FLASH,   curses.COLOR_MAGENTA, -1)
    curses.init_pair(C_WARN,    curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_TAB_ACT, curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(C_TAB_INA, curses.COLOR_BLACK,   curses.COLOR_WHITE)
    curses.init_pair(C_ACCENT,  curses.COLOR_CYAN,    curses.COLOR_BLACK)

def S(win, y, x, text, attr=0):
    """safe_addstr — clips to window bounds."""
    fh, fw = win.getmaxyx()
    if y < 0 or y >= fh or x < 0 or x >= fw:
        return
    clip = fw - x - 1
    if clip <= 0:
        return
    try:
        win.addstr(y, x, text[:clip], attr)
    except curses.error:
        pass

def hline(win, y, w, ch="─"):
    S(win, y, 0, ch * (w - 1))

# ── Shared header (drawn in all views) ────────────────────────────────────────
def draw_header(scr, ci: ChainInfo, view: int, flash: bool, w: int) -> int:
    """Draws title bar + tab bar + optional flash + chain summary. Returns next row."""
    row = 0
    now_str = time.strftime("%Y-%m-%d  %H:%M:%S")
    title = "  ░▒▓  GREGCOIN  (GRC)  MINING  DASHBOARD  ▓▒░"
    pad = max(0, w - len(title) - len(now_str) - 2)
    S(scr, row, 0, (title + " " * pad + now_str + " ")[:w - 1],
      curses.color_pair(C_HEADER) | curses.A_BOLD)
    row += 1

    # Tab bar
    x = 2
    for i, name in enumerate(VIEWS):
        label = f"  {name}  "
        attr = (curses.color_pair(C_TAB_ACT) | curses.A_BOLD) if i == view \
               else curses.color_pair(C_TAB_INA)
        S(scr, row, x, label, attr)
        x += len(label) + 1
    hint = " Tab/F1-F3 "
    S(scr, row, w - len(hint) - 1, hint, curses.color_pair(C_DIM))
    row += 1

    # New-block flash
    if flash:
        msg = f"  ★  NEW BLOCK MINED!  #{ci.blocks}  +100 GRC  ★  "
        S(scr, row, 0, (msg + " " * max(0, w - len(msg) - 1))[:w - 1],
          curses.color_pair(C_FLASH) | curses.A_BOLD)
        row += 1

    # Chain summary strip
    if ci.error:
        S(scr, row, 2, f"Chain  ERROR — {ci.error}", curses.color_pair(C_STOPPED))
    else:
        S(scr, row, 2, "Chain  ", curses.color_pair(C_LABEL) | curses.A_BOLD)
        S(scr, row, 9,
          f"block #{ci.blocks:<7}  diff: {ci.difficulty:.5f}  "
          f"net: {fmt_khs(ci.net_khs):<12}  bal: {ci.balance:,.4f} GRC")
    row += 1
    return row

# ── Bottom key bar ─────────────────────────────────────────────────────────────
def draw_keybar(scr, h: int, w: int, cpu_pct: int, status_msg: str,
                extra_hint: str = ""):
    if status_msg and h > 2:
        S(scr, h - 2, 0,
          f"  ▶  {status_msg}" + " " * w,
          curses.color_pair(C_MINING) | curses.A_BOLD)
    keys  = f"  [s]tart [x]stop [r]estart [1]25% [2]50% [3]100% [q]uit"
    if extra_hint:
        keys += f"   {extra_hint}"
    rinfo = f"  CPU:{cpu_pct}%  r:{REFRESH_S}s  "
    pad   = max(0, w - len(keys) - len(rinfo) - 1)
    S(scr, h - 1, 0, (keys + " " * pad + rinfo)[:w - 1],
      curses.color_pair(C_KEYS))

# ══════════════════════════════════════════════════════════════════════════════
# VIEW 1 — Overview
# ══════════════════════════════════════════════════════════════════════════════
def draw_overview(scr, ci: ChainInfo, nodes: list, start: int, h: int, w: int,
                  status_msg: str):
    row = start
    wide = w >= 100

    # Pre-compute
    total_khs = sum(n.khs      for n in nodes if n.status == "MINING")
    total_acc = sum(n.accepted  for n in nodes if n.status == "MINING")
    total_rej = sum(n.rejected  for n in nodes if n.status == "MINING")
    n_mining  = sum(1           for n in nodes if n.status == "MINING")

    # Next-block progress bar
    bar_w = max(20, min(60, w - 46))
    bar, pct = progress_bar(ci.last_block_time, bar_w)
    S(scr, row, 2, "Next   ", curses.color_pair(C_LABEL) | curses.A_BOLD)
    prefix = f"{fmt_eta(ci.last_block_time)}  ["
    S(scr, row, 9, prefix)
    col = 9 + len(prefix)
    filled = int(pct * bar_w)
    for i, ch in enumerate(bar):
        S(scr, row, col + i, ch,
          curses.color_pair(C_MINING) if i < filled else curses.color_pair(C_DIM))
    S(scr, row, col + bar_w,
      f"]  last: {fmt_age(ci.last_block_time)}   {n_mining}/{len(nodes)} nodes mining")
    row += 1
    hline(scr, row, w)
    row += 1

    # Node table — column positions
    COL_DOT    = 2
    COL_NAME   = 4
    COL_STATUS = 16
    COL_KHS    = 26
    SBW        = 14          # share-bar width (wide only)
    COL_SPCT   = 38
    COL_SBAR   = 43
    COL_ACC    = COL_SBAR + SBW + 2  # 59
    COL_REJ    = COL_ACC + 9         # 68
    COL_APCT   = COL_REJ + 7         # 75

    # Header row
    if wide:
        S(scr, row, COL_NAME,
          f"{'NODE':<10}  {'STATUS':<8}  {'HASHRATE':>10}",
          curses.color_pair(C_HEADER))
        S(scr, row, COL_SPCT,  "SHARE",          curses.color_pair(C_HEADER))
        S(scr, row, COL_ACC,  f"{'ACC':>8}",      curses.color_pair(C_HEADER))
        S(scr, row, COL_REJ,  f"{'REJ':>6}",      curses.color_pair(C_HEADER))
        S(scr, row, COL_APCT, f"{'ACC%':>6}",     curses.color_pair(C_HEADER))
    else:
        S(scr, row, 0,
          f"  {'NODE':<10}  {'STATUS':<8}  {'HASHRATE':>10}  {'ACC':>9}  {'REJ':>8}"[:w-1],
          curses.color_pair(C_HEADER))
    row += 1

    for ns in nodes:
        dot, sc = _node_style(ns.status)
        S(scr, row, COL_DOT,    dot, sc)
        S(scr, row, COL_NAME,   f"{ns.name:<10}")
        S(scr, row, COL_STATUS, f"{ns.status:<8}", sc)
        if ns.status == "MINING":
            S(scr, row, COL_KHS, f"{fmt_khs(ns.khs):>10}")
            if wide:
                share = (ns.khs / total_khs) if total_khs > 0 else 0.0
                S(scr, row, COL_SPCT, f"{share*100:4.0f}%", curses.color_pair(C_LABEL))
                sbar = share_bar(share, SBW)
                for i, ch in enumerate(sbar):
                    S(scr, row, COL_SBAR + i, ch,
                      curses.color_pair(C_MINING) if ch == "█" else curses.color_pair(C_DIM))
                S(scr, row, COL_ACC, f"{ns.accepted:>8}")
                S(scr, row, COL_REJ, f"{ns.rejected:>6}",
                  curses.color_pair(C_STOPPED) if ns.rejected > 0 else 0)
                tot = ns.accepted + ns.rejected
                ap  = (100.0 * ns.accepted / tot) if tot else 100.0
                S(scr, row, COL_APCT, f"{ap:5.1f}%",
                  curses.color_pair(C_STOPPED) if ap < 95 else 0)
            else:
                S(scr, row, 38, f"{ns.accepted:>9}")
                S(scr, row, 49, f"{ns.rejected:>8}",
                  curses.color_pair(C_STOPPED) if ns.rejected > 0 else 0)
        row += 1

    # Totals
    hline(scr, row, w, "─")
    row += 1
    S(scr, row, COL_NAME, f"{'TOTAL':<10}", curses.A_BOLD)
    S(scr, row, COL_KHS,  f"{fmt_khs(total_khs):>10}", curses.A_BOLD)
    if wide:
        S(scr, row, COL_ACC, f"{total_acc:>8}", curses.A_BOLD)
        S(scr, row, COL_REJ, f"{total_rej:>6}",
          (curses.color_pair(C_STOPPED) | curses.A_BOLD) if total_rej else curses.A_BOLD)
        tot = total_acc + total_rej
        ap  = (100.0 * total_acc / tot) if tot else 100.0
        S(scr, row, COL_APCT, f"{ap:5.1f}%",
          (curses.color_pair(C_STOPPED) | curses.A_BOLD) if ap < 95 else curses.A_BOLD)
    else:
        S(scr, row, 38, f"{total_acc:>9}", curses.A_BOLD)
        S(scr, row, 49, f"{total_rej:>8}",
          (curses.color_pair(C_STOPPED) | curses.A_BOLD) if total_rej else curses.A_BOLD)
    row += 1
    hline(scr, row, w)
    row += 1

    # Recent blocks — expand to fill remaining space
    bot = 1 + (1 if status_msg else 0)   # keybar + maybe status
    avail = max(0, h - row - 1 - bot)    # -1 for label row
    n_show = min(len(ci.recent_blocks), MAX_BLOCKS, avail)

    S(scr, row, 2,
      f"RECENT BLOCKS  (last {n_show})" if n_show else "RECENT BLOCKS",
      curses.color_pair(C_LABEL) | curses.A_BOLD)
    row += 1

    if ci.recent_blocks and n_show > 0:
        for blk in ci.recent_blocks[:n_show]:
            hx  = shorten(blk["hash"], 10, 10) if w >= 100 else shorten(blk["hash"], 8, 8)
            ntx = f"  {blk.get('ntx','?')} tx" if w >= 100 else ""
            S(scr, row, 2,
              f"  #{blk['height']:<8}  {fmt_age(blk['time']):<17}  {hx}  "
              f"diff {blk['diff']:.5f}  100 GRC{ntx}")
            row += 1
    else:
        S(scr, row, 4, "No blocks yet — mining in progress…",
          curses.color_pair(C_DIM))
        row += 1

    sep = h - 2 - (1 if status_msg else 0)
    if sep > row:
        hline(scr, sep, w)

# ══════════════════════════════════════════════════════════════════════════════
# VIEW 2 — Transactions
# ══════════════════════════════════════════════════════════════════════════════
def draw_transactions(scr, ci: ChainInfo, txs: list, start: int, h: int, w: int,
                      status_msg: str, scroll: int) -> int:
    """Returns updated scroll value."""
    row = start
    wide = w >= 110

    # Column header
    addr_col = 72 if wide else 60
    S(scr, row, 0,
      f"  {'TYPE':<9} {'BLOCK':<7} {'AGE':<17} {'TXID':<22} {'AMOUNT':>13}  ADDRESS"[:w-1],
      curses.color_pair(C_HEADER))
    row += 1

    bot = 1 + (1 if status_msg else 0)
    content_h = max(0, h - row - bot)

    if not txs:
        S(scr, row, 4,
          "No transactions fetched yet — switch to this view to load…",
          curses.color_pair(C_DIM))
        return scroll

    # Each tx = one summary line + indented outputs for non-coinbase
    lines = []   # list of (render_fn, args)
    for tx in txs:
        lines.append(("tx", tx))
        if not tx.is_coinbase:
            for out in tx.outputs:
                lines.append(("out", out))

    max_scroll = max(0, len(lines) - content_h)
    scroll = min(scroll, max_scroll)

    for item in lines[scroll: scroll + content_h]:
        if row >= h - bot:
            break
        if item[0] == "tx":
            tx = item[1]
            kind = "COINBASE" if tx.is_coinbase else "TRANSFER"
            kattr = curses.color_pair(C_MINING) if tx.is_coinbase else curses.color_pair(C_LABEL)
            short_id = shorten(tx.txid, 10, 10)
            amt = f"{tx.total_out:>11.4f} GRC"
            # Primary output address
            addr = ""
            if tx.outputs:
                a = tx.outputs[0].address
                addr = shorten(a, 14, 10) if wide else shorten(a, 10, 8)
            S(scr, row, 0,
              f"  {kind:<9} #{tx.block_height:<6} {fmt_age(tx.block_time):<17} "
              f"{short_id:<22} {amt}  {addr}"[:w-1],
              kattr)
        elif item[0] == "out":
            out = item[1]
            a = shorten(out.address, 18, 12) if wide else shorten(out.address, 14, 10)
            S(scr, row, 6, f"↳  {a:<32}  {out.value:>10.4f} GRC",
              curses.color_pair(C_DIM))
        row += 1

    # Scroll indicator on right edge
    if max_scroll > 0 and content_h > 0:
        ind = start + 1 + int((scroll / max_scroll) * (content_h - 1))
        if ind < h - bot:
            S(scr, ind, w - 2, "▌", curses.color_pair(C_LABEL))

    # Summary footer line
    n_cb = sum(1 for tx in txs if tx.is_coinbase)
    n_tx = len(txs) - n_cb
    total_grc = sum(tx.total_out for tx in txs)
    S(scr, row, 2,
      f"  {len(txs)} transactions in last {len(ci.recent_blocks)} blocks  "
      f"({n_cb} coinbase  {n_tx} transfers)  "
      f"total: {total_grc:.4f} GRC",
      curses.color_pair(C_DIM))
    row += 1

    sep = h - 2 - (1 if status_msg else 0)
    if sep > row:
        hline(scr, sep, w)

    return scroll

# ══════════════════════════════════════════════════════════════════════════════
# VIEW 3 — Mining Stats
# ══════════════════════════════════════════════════════════════════════════════
def draw_mining_stats(scr, ci: ChainInfo, nodes: list,
                      khs_hist: collections.deque,
                      start: int, h: int, w: int, status_msg: str):
    row = start

    total_khs = sum(n.khs      for n in nodes if n.status == "MINING")
    total_acc = sum(n.accepted  for n in nodes if n.status == "MINING")
    total_rej = sum(n.rejected  for n in nodes if n.status == "MINING")
    n_mining  = sum(1           for n in nodes if n.status == "MINING")

    net = ci.net_khs or 0.0001
    share_pct  = total_khs / net * 100 if net > 0 else 0.0
    blks_per_hr = 3600 / BLOCK_TIME_S * (total_khs / net) if net > 0 else 0.0
    grc_per_hr  = blks_per_hr * 100
    blks_total  = int(ci.balance / 100) if ci.balance > 0 else 0

    # ── Stats panel ──
    S(scr, row, 2, "Network", curses.color_pair(C_LABEL) | curses.A_BOLD)
    S(scr, row, 11,
      f"hashrate: {fmt_khs(net):<12}  difficulty: {ci.difficulty:.5f}  "
      f"height: #{ci.blocks}")
    row += 1

    S(scr, row, 2, "Cluster", curses.color_pair(C_LABEL) | curses.A_BOLD)
    S(scr, row, 11,
      f"hashrate: {fmt_khs(total_khs):<12}  share: {share_pct:.1f}%  "
      f"est. {blks_per_hr:.2f} blk/hr  ≈{grc_per_hr:.0f} GRC/hr  "
      f"nodes: {n_mining}/{len(nodes)}")
    row += 1

    tot = total_acc + total_rej
    ap  = (100.0 * total_acc / tot) if tot else 100.0
    S(scr, row, 2, "Shares ", curses.color_pair(C_LABEL) | curses.A_BOLD)
    S(scr, row, 11,
      f"accepted: {total_acc:<8}  rejected: {total_rej:<6}  "
      f"rate: {ap:.2f}%",
      curses.color_pair(C_STOPPED) if ap < 95 else 0)
    row += 1

    S(scr, row, 2, "Wallet ", curses.color_pair(C_LABEL) | curses.A_BOLD)
    S(scr, row, 11,
      f"balance: {ci.balance:,.4f} GRC  "
      f"(~{blks_total} blocks mined total)")
    row += 1

    hline(scr, row, w)
    row += 1

    # ── Hashrate history chart ──
    # Reserve: chart rows + 2 axis rows + 1 sep + node table (header + nodes) + bot
    bot      = 1 + (1 if status_msg else 0)
    node_rows = 1 + len(nodes)    # table header + rows
    chart_h  = max(3, min(10, h - row - 2 - node_rows - 2 - bot))
    chart_w  = max(20, w - 12)
    hist     = list(khs_hist)
    max_v    = max(hist) if hist else 1.0

    S(scr, row, 2, "HASHRATE HISTORY",
      curses.color_pair(C_LABEL) | curses.A_BOLD)
    S(scr, row, 20,
      f"  peak {fmt_khs(max_v)}   last {len(hist)*REFRESH_S}s   "
      f"({chart_h} rows × {chart_w} cols)",
      curses.color_pair(C_DIM))
    row += 1

    lines = bar_chart(hist, chart_w, chart_h)
    for li, line in enumerate(lines):
        if li == 0:
            lbl = f"{fmt_khs(max_v):>9} "
        elif li == chart_h - 1:
            lbl = f"{'0':>9} "
        else:
            mid = max_v * (chart_h - 1 - li) / (chart_h - 1) if chart_h > 1 else 0
            lbl = f"{fmt_khs(mid):>9} " if li == chart_h // 2 else " " * 10
        S(scr, row, 0, lbl, curses.color_pair(C_DIM))
        for ci_x, ch in enumerate(line):
            S(scr, row, 10 + ci_x, ch,
              curses.color_pair(C_MINING) if ch == "█" else curses.color_pair(C_DIM))
        row += 1

    # X-axis
    S(scr, row, 10, "─" * min(chart_w, w - 11), curses.color_pair(C_DIM))
    S(scr, row, 10, f"← {len(hist)*REFRESH_S}s ago", curses.color_pair(C_DIM))
    S(scr, row, max(11, w - 8), "now →", curses.color_pair(C_DIM))
    row += 1

    hline(scr, row, w)
    row += 1

    # ── Per-node detail ──
    S(scr, row, 0,
      f"  {'NODE':<10}  {'STATUS':<8}  {'KH/s':>9}  {'SHARE':>6}  "
      f"{'ACC':>8}  {'REJ':>6}  {'ACC%':>6}"[:w-1],
      curses.color_pair(C_HEADER))
    row += 1

    for ns in nodes:
        dot, sc = _node_style(ns.status)
        S(scr, row, 2, dot, sc)
        S(scr, row, 4, f"{ns.name:<10}")
        S(scr, row, 16, f"{ns.status:<8}", sc)
        if ns.status == "MINING":
            share = (ns.khs / total_khs * 100) if total_khs > 0 else 0.0
            t2    = ns.accepted + ns.rejected
            ap2   = (100.0 * ns.accepted / t2) if t2 else 100.0
            S(scr, row, 26, f"{ns.khs:>9.1f}")
            S(scr, row, 37, f"{share:>5.1f}%", curses.color_pair(C_LABEL))
            S(scr, row, 45, f"{ns.accepted:>8}")
            S(scr, row, 55, f"{ns.rejected:>6}",
              curses.color_pair(C_STOPPED) if ns.rejected > 0 else 0)
            S(scr, row, 63, f"{ap2:>6.1f}%",
              curses.color_pair(C_STOPPED) if ap2 < 95 else 0)
        row += 1

    sep = h - 2 - (1 if status_msg else 0)
    if sep > row:
        hline(scr, sep, w)

# ══════════════════════════════════════════════════════════════════════════════
# VIEW 4 — Pool Miners
# ══════════════════════════════════════════════════════════════════════════════
def draw_pool_miners(scr, pool: dict, start: int, h: int, w: int,
                     status_msg: str, new_miner_flash: bool,
                     new_miner_addr: str):
    row = start

    if new_miner_flash:
        short = new_miner_addr[:16] + "…" + new_miner_addr[-8:] if len(new_miner_addr) > 26 else new_miner_addr
        msg = f"  ★  NEW MINER CONNECTED:  {short}  ★  "
        S(scr, row, 0, (msg + " " * max(0, w - len(msg) - 1))[:w - 1],
          curses.color_pair(C_FLASH) | curses.A_BOLD)
        row += 1

    error = pool.get("error")
    total = pool.get("total_miners", 0)
    pending = pool.get("pending_connections", 0)
    total_khs = pool.get("total_hashrate_khs", 0.0)

    # Summary strip
    S(scr, row, 2, "Pool   ", curses.color_pair(C_LABEL) | curses.A_BOLD)
    if error:
        S(scr, row, 9, f"grc-pool service offline — start with: systemctl --user start grc-pool",
          curses.color_pair(C_WARN))
    else:
        S(scr, row, 9,
          f"{total} miner{'s' if total != 1 else ''} connected"
          f"  ({pending} pending auth)"
          f"   total external: {fmt_khs(total_khs)}")
    row += 1

    # Onion address reminder
    S(scr, row, 2, "Addr   ", curses.color_pair(C_LABEL) | curses.A_BOLD)
    S(scr, row, 9, "tdeva2kqkihornna6fhon5cddrevqwwndge46qglbh45f3fzgoux7vyd.onion:3333",
      curses.color_pair(C_DIM))
    row += 1
    hline(scr, row, w)
    row += 1

    miners = pool.get("miners", [])
    if not miners:
        S(scr, row, 4,
          "No external miners connected yet. Share the pool .onion address!",
          curses.color_pair(C_DIM))
        row += 1
    else:
        # Header
        S(scr, row, 0,
          f"  {'ADDRESS':<36}  {'HASHRATE':>10}  {'ACC':>6}  {'REJ':>4}  "
          f"{'LAST SHARE':>12}  {'CONNECTED':>12}"[:w - 1],
          curses.color_pair(C_HEADER))
        row += 1

        bot = 1 + (1 if status_msg else 0)
        for m in miners:
            if row >= h - bot:
                break
            addr = m.get("address", "")
            short_addr = addr[:18] + "…" + addr[-14:] if len(addr) > 34 else f"{addr:<36}"
            khs = m.get("hashrate_khs", 0.0)
            acc = m.get("shares_accepted", 0)
            rej = m.get("shares_rejected", 0)
            last = m.get("last_share_age")
            last_str = f"{last:.0f}s ago" if last is not None else "none yet"
            connected = m.get("connected_for", 0)
            conn_str = (f"{connected//60}m{connected%60:02d}s" if connected >= 60
                        else f"{connected}s")
            khs_str = fmt_khs(khs) if khs > 0 else "—"
            S(scr, row, 2, short_addr, curses.color_pair(C_MINING) | curses.A_BOLD)
            S(scr, row, 40, f"{khs_str:>10}")
            S(scr, row, 52, f"{acc:>6}")
            S(scr, row, 60, f"{rej:>4}",
              curses.color_pair(C_STOPPED) if rej > 0 else 0)
            S(scr, row, 66, f"{last_str:>12}", curses.color_pair(C_DIM))
            S(scr, row, 80, f"{conn_str:>12}", curses.color_pair(C_DIM))
            row += 1

        hline(scr, row, w)
        row += 1
        S(scr, row, 2, f"TOTAL  ", curses.color_pair(C_LABEL) | curses.A_BOLD)
        S(scr, row, 9, f"{fmt_khs(total_khs):>10}  external hashrate  "
          f"({total} miner{'s' if total != 1 else ''})",
          curses.A_BOLD)
        row += 1

    sep = h - 2 - (1 if status_msg else 0)
    if sep > row:
        hline(scr, sep, w)

# ── Shared helper ──────────────────────────────────────────────────────────────
def _node_style(status: str):
    if status == "MINING":
        return "●", curses.color_pair(C_MINING) | curses.A_BOLD
    if status == "STOPPED":
        return "○", curses.color_pair(C_STOPPED)
    return "?", curses.color_pair(C_OFFLINE)

# ── Main loop ──────────────────────────────────────────────────────────────────
def main(stdscr):
    init_colors()
    curses.curs_set(0)
    curses.halfdelay(REFRESH_S * 10)

    ci                = ChainInfo()
    node_list         = discover_nodes()
    nodes             = [NodeStats(name=n[0], api_ip=n[1]) for n in node_list]
    khs_hist          = collections.deque(maxlen=KHS_HISTORY)
    txs: list         = []
    pool_data: dict   = {"miners": [], "total_miners": 0, "total_hashrate_khs": 0.0}
    last_fetch        = 0.0
    last_discover     = 0.0
    last_pool_fetch   = 0.0
    last_blocks       = 0
    last_tx_blocks    = -1
    status_msg        = ""
    status_until      = 0.0
    cpu_pct           = 100
    flash_until       = 0.0
    new_miner_until   = 0.0
    new_miner_addr    = ""
    known_miners: set = set()
    view              = 0    # 0=Overview 1=Transactions 2=Mining Stats 3=Pool Miners
    tx_scroll         = 0

    while True:
        now  = time.time()
        h, w = stdscr.getmaxyx()

        # ── Node discovery refresh ──
        if now - last_discover >= NODE_REDISCOVER_S:
            new_list = discover_nodes()
            if [n[0] for n in new_list] != [n[0] for n in node_list]:
                # Merge — preserve existing stats for known nodes
                existing = {ns.name: ns for ns in nodes}
                node_list = new_list
                nodes = [existing.get(n[0], NodeStats(name=n[0], api_ip=n[1]))
                         for n in node_list]
                for i, (name, api_ip) in enumerate(node_list):
                    nodes[i].api_ip = api_ip
            last_discover = now

        # ── Data refresh ──
        if now - last_fetch >= REFRESH_S:
            ci = fetch_chain()
            total = 0.0
            for i, (name, api_ip) in enumerate(node_list):
                ns = fetch_miner(api_ip)
                ns.name = name
                nodes[i] = ns
                if ns.status == "MINING":
                    total += ns.khs
            khs_hist.append(total)
            if ci.blocks > last_blocks > 0:
                flash_until = now + 10
            last_blocks = ci.blocks
            last_fetch  = now

        # ── Pool miners refresh (every 5s) ──
        if now - last_pool_fetch >= 5:
            pool_data = fetch_pool_miners()
            current_miners = {m["address"] for m in pool_data.get("miners", [])
                              if m.get("address", "").startswith("grc1")}
            new_ones = current_miners - known_miners
            if new_ones and known_miners:  # don't flash on first load
                new_miner_addr = next(iter(new_ones))
                new_miner_until = now + 15
                curses.beep()
            known_miners = current_miners
            last_pool_fetch = now

        # Fetch tx detail lazily (only when on Transactions view and blocks changed)
        if view == 1 and ci.blocks != last_tx_blocks and ci.recent_blocks:
            txs = fetch_transactions(ci)
            last_tx_blocks = ci.blocks

        if now > status_until:
            status_msg = ""

        flash = now < flash_until
        new_miner_flash = now < new_miner_until

        # ── Draw ──
        stdscr.erase()
        content_start = draw_header(stdscr, ci, view, flash, w)

        if view == 0:
            draw_overview(stdscr, ci, nodes, content_start, h, w, status_msg)
            hint = ""
        elif view == 1:
            tx_scroll = draw_transactions(
                stdscr, ci, txs, content_start, h, w, status_msg, tx_scroll)
            hint = "[↑↓ PgUp PgDn scroll]"
        elif view == 2:
            draw_mining_stats(stdscr, ci, nodes, khs_hist, content_start, h, w, status_msg)
            hint = ""
        else:
            draw_pool_miners(stdscr, pool_data, content_start, h, w,
                             status_msg, new_miner_flash, new_miner_addr)
            hint = ""

        draw_keybar(stdscr, h, w, cpu_pct, status_msg, hint)
        stdscr.refresh()

        # ── Input ──
        try:
            key = stdscr.getch()
        except curses.error:
            key = -1

        if key in (ord('q'), ord('Q'), 27):
            break
        # Tab / Shift+Tab cycle views
        elif key in (9, ord('\t')):                 # Tab
            view = (view + 1) % len(VIEWS)
            tx_scroll = 0
        elif key == curses.KEY_BTAB:               # Shift+Tab
            view = (view - 1) % len(VIEWS)
            tx_scroll = 0
        elif key == curses.KEY_F1:
            view = 0
        elif key == curses.KEY_F2:
            view = 1; tx_scroll = 0
        elif key == curses.KEY_F3:
            view = 2
        elif key == curses.KEY_F4:
            view = 3
        elif key in (ord('p'), ord('P')):
            view = 3
        # Scroll in Transactions view
        elif key == curses.KEY_UP   and view == 1:
            tx_scroll = max(0, tx_scroll - 1)
        elif key == curses.KEY_DOWN and view == 1:
            tx_scroll += 1
        elif key == curses.KEY_PPAGE and view == 1:
            tx_scroll = max(0, tx_scroll - (h // 2))
        elif key == curses.KEY_NPAGE and view == 1:
            tx_scroll += h // 2
        # Miner control (works in any view)
        elif key in (ord('s'), ord('S')):
            run_ctl("start", cpu_pct)
            status_msg = f"Starting all miners at {cpu_pct}% CPU…"
            status_until = now + 5; last_fetch = 0
        elif key in (ord('x'), ord('X')):
            run_ctl("stop")
            status_msg = "Stopping all miners…"
            status_until = now + 5; last_fetch = 0
        elif key in (ord('r'), ord('R')):
            run_ctl("restart", cpu_pct)
            status_msg = f"Restarting all miners at {cpu_pct}% CPU…"
            status_until = now + 8
        elif key == ord('1'):
            cpu_pct = 25; run_ctl("restart", cpu_pct)
            status_msg = "Throttling to 25% CPU (1 thread/node)…"
            status_until = now + 8; last_fetch = 0
        elif key == ord('2'):
            cpu_pct = 50; run_ctl("restart", cpu_pct)
            status_msg = "Throttling to 50% CPU (2 threads/node)…"
            status_until = now + 8; last_fetch = 0
        elif key == ord('3'):
            cpu_pct = 100; run_ctl("restart", cpu_pct)
            status_msg = "Full throttle — 100% CPU (all threads)…"
            status_until = now + 8; last_fetch = 0

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
