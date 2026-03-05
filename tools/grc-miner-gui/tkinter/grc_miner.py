#!/usr/bin/env python3
"""
Gregcoin (GRC) GUI Miner — Python/tkinter
Connects to a running bitcoind node, mines blocks via getblocktemplate,
and displays live stats.

Requirements: Python 3.8+, no external dependencies (uses only stdlib).

Usage:
  python3 grc_miner.py

Package as single executable:
  pip install pyinstaller
  pyinstaller --onefile --windowed grc_miner.py
"""

import hashlib
import json
import multiprocessing
import os
import struct
import time
import tkinter as tk
import tkinter.ttk as ttk
import urllib.request
import urllib.error
import base64
import threading
from queue import Queue, Empty

APP_NAME = "Gregcoin Miner"
VERSION  = "0.1.0"

# ── SHA256d ───────────────────────────────────────────────────────────────────

def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


# ── Bitcoin / Gregcoin serialisation helpers ──────────────────────────────────

def varint(n: int) -> bytes:
    if n < 0xfd:
        return struct.pack('<B', n)
    elif n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    elif n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    else:
        return b'\xff' + struct.pack('<Q', n)


def merkle_root(txids: list) -> bytes:
    """Compute merkle root from list of raw txid bytes (little-endian)."""
    if not txids:
        return b'\x00' * 32
    layer = list(txids)
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]


def bits_to_target_int(nbits_hex: str) -> int:
    nbits = int(nbits_hex, 16)
    exp  = (nbits >> 24) & 0xff
    mant = nbits & 0x007fffff
    return mant * (256 ** (exp - 3))


def build_coinbase(height: int, coinbase_value: int, address_script: bytes,
                   extra_nonce: int = 0) -> bytes:
    """Build a minimal coinbase transaction."""
    # scriptSig: BIP34 height + extra_nonce
    height_bytes = height.to_bytes((height.bit_length() + 8) // 8, 'little')
    script_sig = bytes([len(height_bytes)]) + height_bytes
    if extra_nonce:
        en = extra_nonce.to_bytes(4, 'little')
        script_sig += bytes([len(en)]) + en

    script_pubkey = address_script

    tx = (
        struct.pack('<i', 1) +
        varint(1) +
        b'\x00' * 32 + struct.pack('<I', 0xffffffff) +
        varint(len(script_sig)) + script_sig +
        struct.pack('<I', 0xffffffff) +
        varint(1) +
        struct.pack('<q', coinbase_value) +
        varint(len(script_pubkey)) + script_pubkey +
        struct.pack('<I', 0)
    )
    return tx


def p2pkh_script(address: str) -> bytes:
    """Minimal OP_DUP OP_HASH160 <20 bytes> OP_EQUALVERIFY OP_CHECKSIG.
       Decodes base58check address to extract hash160.
    """
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n = 0
    for char in address:
        n = n * 58 + alphabet.index(char)
    raw = n.to_bytes(25, 'big')
    # raw = version(1) + hash160(20) + checksum(4)
    hash160 = raw[1:21]
    return (b'\x76\xa9\x14' + hash160 + b'\x88\xac')


# ── RPC client ────────────────────────────────────────────────────────────────

class RPCClient:
    def __init__(self, host, port, user, password):
        self.url = f"http://{host}:{port}/"
        self.auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._id = 0

    def call(self, method, *params):
        self._id += 1
        payload = json.dumps({
            "jsonrpc": "1.1",
            "id": self._id,
            "method": method,
            "params": list(params),
        }).encode()
        req = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Basic {self.auth}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result["result"]


# ── Mining worker (runs in a separate thread) ─────────────────────────────────

def mine_block_range(header_76: bytes, nbits_hex: str,
                     start: int, end: int,
                     result_q: Queue, stop_event: threading.Event):
    target = bits_to_target_int(nbits_hex)
    hdr = bytearray(header_76 + b'\x00\x00\x00\x00')
    nonce = start
    count = 0
    t0 = time.monotonic()
    while nonce <= end and not stop_event.is_set():
        struct.pack_into('<I', hdr, 76, nonce)
        h = sha256d(bytes(hdr))
        val = int.from_bytes(h[::-1], 'big')
        if val < target:
            result_q.put(('found', nonce, h[::-1].hex()))
            return
        nonce += 1
        count += 1
        if count % 10_000 == 0:
            elapsed = time.monotonic() - t0
            rate = count / elapsed if elapsed > 0 else 0
            result_q.put(('hashrate', rate))
    result_q.put(('exhausted', start, end))


class MinerEngine:
    def __init__(self, rpc: RPCClient, address: str, on_event):
        self.rpc = rpc
        self.address = address
        self.on_event = on_event
        self._thread = None
        self._stop = threading.Event()
        self._result_q = Queue()
        self.running = False
        self.extra_nonce = 0

    def start(self):
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.running = False

    def _run_loop(self):
        while not self._stop.is_set():
            try:
                template = self.rpc.call("getblocktemplate",
                                         {"rules": ["segwit"]})
            except Exception as e:
                self.on_event("error", str(e))
                time.sleep(5)
                continue

            self.extra_nonce += 1
            try:
                header_76, full_block_prefix = self._build_header(template)
            except Exception as e:
                self.on_event("error", f"Build header: {e}")
                time.sleep(2)
                continue

            q = Queue()
            t = threading.Thread(
                target=mine_block_range,
                args=(header_76, template["bits"], 0, 0xffffffff, q, self._stop),
                daemon=True,
            )
            t.start()

            while t.is_alive() or not q.empty():
                try:
                    msg = q.get(timeout=0.2)
                except Empty:
                    continue
                if msg[0] == 'found':
                    nonce = msg[1]
                    block_hash = msg[2]
                    # Serialize and submit the full block
                    nonce_bytes = struct.pack('<I', nonce)
                    block_hex = (full_block_prefix + nonce_bytes.hex())
                    try:
                        self.rpc.call("submitblock", block_hex)
                        self.on_event("block_found", block_hash)
                    except Exception as e:
                        self.on_event("error", f"Submit: {e}")
                    break
                elif msg[0] == 'hashrate':
                    self.on_event("hashrate", msg[1])
                elif msg[0] == 'exhausted':
                    # All nonces tried without finding — get new template
                    break

    def _build_header(self, template) -> tuple:
        height = template["height"]
        coinbase_value = template["coinbasevalue"]
        nbits = template["bits"]
        prev_hash = bytes.fromhex(template["previousblockhash"])[::-1]
        version = template["version"]
        curtime = template["curtime"]

        address_script = p2pkh_script(self.address)
        cb_tx = build_coinbase(height, coinbase_value, address_script,
                               self.extra_nonce)
        cb_txid = sha256d(cb_tx)

        tx_data = [cb_tx]
        txids   = [cb_txid]
        for tx in template.get("transactions", []):
            raw = bytes.fromhex(tx["data"])
            tx_data.append(raw)
            txids.append(bytes.fromhex(tx["txid"])[::-1])

        mr = merkle_root(txids)

        header_76 = (
            struct.pack('<I', version) +
            prev_hash +
            mr +
            struct.pack('<I', curtime) +
            bytes.fromhex(nbits)[::-1]
        )
        assert len(header_76) == 76

        # Full block prefix (header + tx count + all tx data)
        block_prefix_bytes = (
            header_76 +
            b'\x00\x00\x00\x00' +   # nNonce placeholder (we'll overwrite)
            varint(len(tx_data)) +
            b''.join(tx_data)
        )
        # Return header (without nNonce) and full block as hex (without nNonce at end)
        full_prefix_hex = block_prefix_bytes[:-len(b''.join(tx_data)) -
                                              len(varint(len(tx_data))) - 4].hex()
        # Actually simpler: build the part before nNonce and after separately
        before_nonce = header_76.hex()
        after_nonce  = (varint(len(tx_data)) + b''.join(tx_data)).hex()
        combined_prefix = before_nonce + "00000000" + after_nonce
        # We'll reconstruct after finding nNonce
        full_block_prefix_hex = before_nonce + after_nonce  # nNonce inserted at offset 76
        # Return header_76 and the full block parts for later assembly
        return header_76, (before_nonce, after_nonce)


# ── GUI ───────────────────────────────────────────────────────────────────────

GRC_GREEN  = "#00c853"
GRC_DARK   = "#1a1a2e"
GRC_PANEL  = "#16213e"
GRC_ACCENT = "#0f3460"
GRC_TEXT   = "#e0e0e0"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{VERSION}")
        self.configure(bg=GRC_DARK)
        self.resizable(False, False)

        self.rpc: RPCClient | None = None
        self.engine: MinerEngine | None = None
        self.hash_rate = 0.0
        self.blocks_found = 0
        self.start_time: float | None = None

        self._build_ui()
        self._poll_stats()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        pad = dict(padx=12, pady=6)

        # Title bar
        title_frame = tk.Frame(self, bg=GRC_ACCENT)
        title_frame.pack(fill='x')
        tk.Label(title_frame, text="⛏  GREGCOIN MINER", font=("Helvetica", 16, "bold"),
                 bg=GRC_ACCENT, fg=GRC_GREEN).pack(pady=8)

        # Connection settings
        conn_frame = tk.LabelFrame(self, text="Node Connection", bg=GRC_PANEL,
                                   fg=GRC_TEXT, font=("Helvetica", 10))
        conn_frame.pack(fill='x', **pad)

        row = 0
        for label, default, attr in [
            ("Host",     "127.0.0.1", "host_var"),
            ("RPC Port", "8445",      "port_var"),
            ("User",     "grcuser",   "user_var"),
            ("Password", "",          "pass_var"),
        ]:
            tk.Label(conn_frame, text=label+":", bg=GRC_PANEL, fg=GRC_TEXT,
                     width=10, anchor='e').grid(row=row, column=0, **pad)
            v = tk.StringVar(value=default)
            setattr(self, attr, v)
            show = '*' if label == "Password" else ''
            tk.Entry(conn_frame, textvariable=v, show=show, width=28,
                     bg=GRC_DARK, fg=GRC_TEXT, insertbackground=GRC_TEXT
                     ).grid(row=row, column=1, **pad)
            row += 1

        # Mining address
        tk.Label(conn_frame, text="Address:", bg=GRC_PANEL, fg=GRC_TEXT,
                 width=10, anchor='e').grid(row=row, column=0, **pad)
        self.addr_var = tk.StringVar()
        tk.Entry(conn_frame, textvariable=self.addr_var, width=40,
                 bg=GRC_DARK, fg=GRC_TEXT, insertbackground=GRC_TEXT
                 ).grid(row=row, column=1, **pad)

        # Stats panel
        stats_frame = tk.LabelFrame(self, text="Mining Stats", bg=GRC_PANEL,
                                    fg=GRC_TEXT, font=("Helvetica", 10))
        stats_frame.pack(fill='x', **pad)

        self.hashrate_label = self._stat_row(stats_frame, "Hash Rate",   "0 H/s", 0)
        self.blocks_label   = self._stat_row(stats_frame, "Blocks Found", "0",    1)
        self.balance_label  = self._stat_row(stats_frame, "Balance",      "0 GRC", 2)
        self.uptime_label   = self._stat_row(stats_frame, "Uptime",       "--",    3)
        self.status_label   = self._stat_row(stats_frame, "Status",       "Stopped", 4)

        # Log
        log_frame = tk.LabelFrame(self, text="Log", bg=GRC_PANEL, fg=GRC_TEXT,
                                  font=("Helvetica", 10))
        log_frame.pack(fill='both', expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=8, bg=GRC_DARK, fg=GRC_TEXT,
                                font=("Courier", 9), state='disabled',
                                insertbackground=GRC_TEXT)
        sb = tk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.log_text.pack(fill='both', expand=True)

        # Buttons
        btn_frame = tk.Frame(self, bg=GRC_DARK)
        btn_frame.pack(pady=8)
        self.start_btn = tk.Button(btn_frame, text="▶  Start Mining",
                                   command=self._start_mining,
                                   bg=GRC_GREEN, fg="black",
                                   font=("Helvetica", 11, "bold"), width=16,
                                   relief='flat', cursor='hand2')
        self.start_btn.pack(side='left', padx=6)
        self.stop_btn = tk.Button(btn_frame, text="◼  Stop",
                                  command=self._stop_mining,
                                  bg="#b71c1c", fg="white",
                                  font=("Helvetica", 11, "bold"), width=10,
                                  relief='flat', cursor='hand2', state='disabled')
        self.stop_btn.pack(side='left', padx=6)

    def _stat_row(self, parent, label, value, row):
        tk.Label(parent, text=label+":", bg=GRC_PANEL, fg="#aaa",
                 width=14, anchor='e').grid(row=row, column=0, padx=8, pady=4)
        v = tk.Label(parent, text=value, bg=GRC_PANEL, fg=GRC_GREEN,
                     font=("Courier", 11, "bold"), anchor='w', width=24)
        v.grid(row=row, column=1, padx=8, pady=4)
        return v

    # ── Mining control ────────────────────────────────────────────────────────

    def _start_mining(self):
        address = self.addr_var.get().strip()
        if not address:
            # Try to get a new address from the node
            try:
                rpc = RPCClient(self.host_var.get(), int(self.port_var.get()),
                                self.user_var.get(), self.pass_var.get())
                wallets = rpc.call("listwallets")
                if not wallets:
                    rpc.call("createwallet", "miner")
                address = rpc.call("getnewaddress")
                self.addr_var.set(address)
                self._log(f"Using address: {address}")
            except Exception as e:
                self._log(f"ERROR getting address: {e}")
                return

        try:
            self.rpc = RPCClient(self.host_var.get(), int(self.port_var.get()),
                                 self.user_var.get(), self.pass_var.get())
            info = self.rpc.call("getblockchaininfo")
            self._log(f"Connected to Gregcoin — height {info['blocks']}")
        except Exception as e:
            self._log(f"ERROR connecting: {e}")
            return

        self.start_time = time.monotonic()
        self.blocks_found = 0
        self.engine = MinerEngine(self.rpc, address, self._engine_event)
        self.engine.start()

        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.status_label.config(text="Mining", fg=GRC_GREEN)
        self._log("Mining started")

    def _stop_mining(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
        self.status_label.config(text="Stopped", fg="#ff5722")
        self._log("Mining stopped")

    def _engine_event(self, event_type, data=None):
        if event_type == "hashrate":
            self.hash_rate = data
        elif event_type == "block_found":
            self.blocks_found += 1
            self._log(f"BLOCK FOUND! Hash: {data[:16]}...")
        elif event_type == "error":
            self._log(f"ERROR: {data}")

    # ── Periodic stats refresh ────────────────────────────────────────────────

    def _poll_stats(self):
        if self.engine and self.engine.running:
            rate = self.hash_rate
            if rate >= 1e6:
                rate_str = f"{rate/1e6:.2f} MH/s"
            elif rate >= 1e3:
                rate_str = f"{rate/1e3:.1f} KH/s"
            else:
                rate_str = f"{rate:.0f} H/s"
            self.hashrate_label.config(text=rate_str)
            self.blocks_label.config(text=str(self.blocks_found))

            if self.start_time:
                elapsed = int(time.monotonic() - self.start_time)
                h, m = divmod(elapsed // 60, 60)
                s = elapsed % 60
                self.uptime_label.config(text=f"{h:02d}:{m:02d}:{s:02d}")

            if self.rpc:
                try:
                    bal = self.rpc.call("getbalance")
                    self.balance_label.config(text=f"{bal:.4f} GRC")
                except Exception:
                    pass

        self.after(1000, self._poll_stats)

    def _log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f"[{ts}] {msg}\n")
        self.log_text.see('end')
        self.log_text.configure(state='disabled')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
