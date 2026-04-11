#!/usr/bin/env python3
"""
grc-pool.py — Gregcoin Tor solo stratum pool
Each miner submits their own grc1q... address as username.
When they find a valid block, 100% of the reward goes to their address.
Listens on 127.0.0.1:3333 only — Tor forwards connections in.
"""

import asyncio
import hashlib
import json
import logging
import os
import struct
import time
import urllib.request
import urllib.error
import base64
import argparse
import signal
import sys
from typing import Optional

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("grc-pool")

# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

# ── Bech32 ────────────────────────────────────────────────────────────────────
# Minimal bech32 decoder to validate grc1q... addresses and extract witness program

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32M_CONST = 0x2bc830a3

def _bech32_polymod(values):
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk

def _bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]

def _bech32_verify_checksum(hrp, data):
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + list(data))
    return const == 1 or const == BECH32M_CONST

def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret

def decode_grc_address(addr: str) -> Optional[bytes]:
    """Decode a grc1q... bech32 segwit address. Returns 20-byte witness program or None."""
    if not isinstance(addr, str):
        return None
    addr = addr.lower()
    if not addr.startswith("grc1"):
        return None
    sep = addr.rfind("1")
    if sep < 1:
        return None
    hrp, data_str = addr[:sep], addr[sep + 1:]
    if hrp != "grc":
        return None
    if len(data_str) < 6:
        return None
    try:
        data = [CHARSET.find(c) for c in data_str]
    except Exception:
        return None
    if -1 in data:
        return None
    if not _bech32_verify_checksum(hrp, data):
        return None
    decoded = _convertbits(data[1:-6], 5, 8, False)
    if decoded is None or len(decoded) < 2:
        return None
    witver = data[0]
    if witver != 0:  # only segwit v0 (P2WPKH)
        return None
    if len(decoded) != 20:  # P2WPKH = 20 bytes
        return None
    return bytes(decoded)

def p2wpkh_script(witness_program: bytes) -> bytes:
    """Build P2WPKH scriptPubKey: OP_0 <20-byte-hash>"""
    return b"\x00\x14" + witness_program

# ── SHA256d ───────────────────────────────────────────────────────────────────

def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def swab32(b: bytes) -> bytes:
    """Byte-swap each 32-bit word in a byte string (cpuminer endian compensation)."""
    assert len(b) % 4 == 0, f"swab32: length {len(b)} not divisible by 4"
    result = bytearray()
    for i in range(0, len(b), 4):
        result += b[i:i+4][::-1]
    return bytes(result)

# ── RPC client ────────────────────────────────────────────────────────────────

class RPCError(Exception):
    pass

class RPCClient:
    def __init__(self, host, port, user, password):
        self.url = f"http://{host}:{port}/"
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        }
        self._id = 0

    def call(self, method: str, params=None, timeout=60) -> dict:
        self._id += 1
        payload = json.dumps({
            "jsonrpc": "1.0",
            "id": self._id,
            "method": method,
            "params": params or [],
        }).encode()
        req = urllib.request.Request(self.url, data=payload, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise RPCError(f"HTTP {e.code}: {body}")
        if result.get("error"):
            raise RPCError(str(result["error"]))
        return result["result"]

# ── Coinbase builder ──────────────────────────────────────────────────────────

def varint(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    elif n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    elif n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)

def encode_height(height: int) -> bytes:
    """Encode block height for coinbase scriptSig (BIP34)."""
    if height == 0:
        return b"\x00"
    result = []
    h = height
    while h > 0:
        result.append(h & 0xff)
        h >>= 8
    if result[-1] & 0x80:
        result.append(0x00)
    return bytes([len(result)] + result)

def build_coinbase(height: int, value_sat: int, script_pubkey: bytes,
                   extranonce1: bytes, extranonce2_size: int,
                   pool_tag: str) -> tuple[bytes, bytes]:
    """
    Build coinbase split into (coinbase1, coinbase2).
    extranonce placeholder sits between them inside the scriptsig.
    Returns (coinbase1, coinbase2).
    """
    version = struct.pack("<I", 1)
    vin_count = varint(1)
    # Null prevout (coinbase input)
    prevout = b"\x00" * 32 + b"\xff\xff\xff\xff"
    vin_sequence = b"\xff\xff\xff\xff"

    # ScriptSig: BIP34 height + pool tag + [extranonce goes here]
    height_enc = encode_height(height)
    tag_bytes = pool_tag.encode()
    # scriptsig = height_enc + tag + extranonce1 + extranonce2
    script_prefix = height_enc + varint(len(tag_bytes)) + tag_bytes
    script_total_len = len(script_prefix) + len(extranonce1) + extranonce2_size
    scriptsig_len = varint(script_total_len)

    # Output
    vout_count = varint(1)
    value = struct.pack("<q", value_sat)
    script_pubkey_len = varint(len(script_pubkey))
    locktime = struct.pack("<I", 0)

    coinbase1 = (
        version +
        vin_count +
        prevout +
        scriptsig_len +
        script_prefix
        # miner inserts extranonce1 + extranonce2 here (standard stratum)
    )
    coinbase2 = (
        vin_sequence +
        vout_count +
        value +
        script_pubkey_len +
        script_pubkey +
        locktime
    )
    return coinbase1, coinbase2

# ── Job manager ───────────────────────────────────────────────────────────────

def target_from_bits(bits_hex: str) -> int:
    bits = int(bits_hex, 16)
    exp = bits >> 24
    mant = bits & 0xffffff
    return mant * (1 << (8 * (exp - 3)))

def diff_to_target(diff: float) -> int:
    diff1_target = (1 << 224) - 1  # Bitcoin diff-1 target
    return int(diff1_target / diff)

def le_hex(data: bytes) -> str:
    return data[::-1].hex()

class Job:
    __slots__ = ("job_id", "gbt", "coinbase1", "coinbase2", "merkle_branch",
                 "version", "nbits", "ntime", "network_target", "height")

    def __init__(self, job_id, gbt, coinbase1, coinbase2, merkle_branch):
        self.job_id = job_id
        self.gbt = gbt
        self.coinbase1 = coinbase1
        self.coinbase2 = coinbase2
        self.merkle_branch = merkle_branch
        self.version = gbt["version"]
        self.nbits = gbt["bits"]
        self.ntime = gbt["curtime"]
        self.network_target = target_from_bits(gbt["bits"])
        self.height = gbt["height"]

class JobManager:
    def __init__(self, rpc: RPCClient, cfg: dict):
        self.rpc = rpc
        self.cfg = cfg
        self._current_jobs: dict[str, Job] = {}  # job_id → Job
        self._job_counter = 0
        self._longpoll_id: Optional[str] = None
        self._new_block_event = asyncio.Event()
        self._sessions: list["StratumSession"] = []
        self._lock = asyncio.Lock()
        self._cached_gbt: Optional[dict] = None  # latest GBT from poll_loop

    def register_session(self, session):
        self._sessions.append(session)

    def unregister_session(self, session):
        self._sessions.remove(session)

    def new_job_id(self) -> str:
        self._job_counter += 1
        return f"{self._job_counter:08x}"

    def build_job(self, gbt: dict, miner_address: str, extranonce1: bytes) -> Job:
        witness_program = decode_grc_address(miner_address)
        script_pubkey = p2wpkh_script(witness_program)
        value_sat = gbt["coinbasevalue"]
        height = gbt["height"]
        extranonce2_size = self.cfg["extranonce2_size"]
        pool_tag = self.cfg.get("pool_tag", "/GRCPool/")

        coinbase1, coinbase2 = build_coinbase(
            height, value_sat, script_pubkey,
            extranonce1, extranonce2_size, pool_tag,
        )

        # Build merkle branch (list of tx hashes, not including coinbase)
        merkle_branch = [tx["hash"] for tx in gbt.get("transactions", [])]

        job_id = self.new_job_id()
        job = Job(job_id, gbt, coinbase1, coinbase2, merkle_branch)
        self._current_jobs[job_id] = job
        # Prune old jobs (keep last 20)
        if len(self._current_jobs) > 20:
            oldest = list(self._current_jobs.keys())[0]
            del self._current_jobs[oldest]
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self._current_jobs.get(job_id)

    def get_stats(self) -> dict:
        authorized = [s for s in self._sessions if s.authorized]
        total_khs = sum(s.hashrate_khs() for s in authorized)
        return {
            "miners": [s.to_stats() for s in authorized],
            "total_miners": len(authorized),
            "total_hashrate_khs": round(total_khs, 2),
            "pending_connections": len(self._sessions) - len(authorized),
        }

    async def fetch_gbt(self, longpollid: Optional[str] = None) -> dict:
        params: list = [{"rules": ["segwit"]}]
        if longpollid:
            params[0]["longpollid"] = longpollid
        loop = asyncio.get_event_loop()
        gbt = await loop.run_in_executor(None, lambda: self.rpc.call("getblocktemplate", params, timeout=120))
        return gbt

    async def poll_loop(self):
        """Main GBT polling loop with long-poll support."""
        log.info("Job manager starting, fetching first GBT...")
        while True:
            try:
                gbt = await self.fetch_gbt(self._longpoll_id)
                self._longpoll_id = gbt.get("longpollid")
                self._cached_gbt = gbt
                log.info(f"New block template: height={gbt['height']} txs={len(gbt.get('transactions',[]))}")
                self._new_block_event.set()
                self._new_block_event.clear()
                # Notify all sessions of new work
                for session in list(self._sessions):
                    asyncio.ensure_future(session.send_new_work(clean=True))
                # If no longpoll, wait before re-polling
                if not self._longpoll_id:
                    await asyncio.sleep(self.cfg["job_refresh_seconds"])
            except RPCError as e:
                log.warning(f"RPC error fetching GBT: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                log.warning(f"GBT poll error: {e}")
                await asyncio.sleep(5)

    async def get_current_gbt(self) -> dict:
        """Return cached GBT if available, else fetch fresh. Only poll_loop fetches live."""
        if self._cached_gbt is not None:
            return self._cached_gbt
        # First miner connected before poll_loop returned — fetch once
        gbt = await self.fetch_gbt()
        self._cached_gbt = gbt
        return gbt

# ── Stratum session ───────────────────────────────────────────────────────────

SHARE_DIFFICULTY = None  # set from config at startup

class StratumSession:
    extranonce1_counter = 0

    def __init__(self, reader, writer, manager: JobManager, cfg: dict):
        self.reader = reader
        self.writer = writer
        self.manager = manager
        self.cfg = cfg
        self.miner_address: Optional[str] = None
        self.extranonce1 = self._new_extranonce1()
        self.subscribed = False
        self.authorized = False
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self.peer = writer.get_extra_info("peername")
        self._current_job: Optional[Job] = None
        self.connect_time = time.time()
        self.shares_accepted = 0
        self.shares_rejected = 0
        self._share_log: list = []  # list of (timestamp, difficulty)
        log.info(f"New connection from {self.peer} extranonce1={self.extranonce1.hex()}")

    def _new_extranonce1(self) -> bytes:
        StratumSession.extranonce1_counter += 1
        return struct.pack(">I", StratumSession.extranonce1_counter)

    # ── Send helpers ──────────────────────────────────────────────────────────

    async def send(self, msg: dict):
        data = json.dumps(msg) + "\n"
        self.writer.write(data.encode())
        await self.writer.drain()

    async def respond(self, req_id, result=None, error=None):
        await self.send({"id": req_id, "result": result, "error": error})

    async def notify(self, job: Job, clean: bool):
        """Send mining.notify for a job."""
        # Encode coinbase parts as hex
        cb1 = job.coinbase1.hex()
        cb2 = job.coinbase2.hex()
        # cpuminer uses SHA256 with swap=0 (each 32-bit word byte-reversed on LE machines).
        # To ensure the miner's effective SHA256 operates on the correct Bitcoin block header
        # bytes, we pre-compensate each field:
        #   version: send BE so miner's le32dec + swap=0 gives correct LE bytes
        #   version: send BE so miner's le32dec + swap=0 gives correct LE bytes
        #   prevhash: send swab32(internal) so miner's hex2bin + swap=0 gives internal bytes
        #   ntime: send BE (unchanged) — miner's le32dec + swap=0 already gives correct LE
        #   nbits: send AS-IS from GBT so miner's le32dec + swap=0 gives correct LE bytes
        #     (le32dec("1d06b2b6") = 0xb6b2061d; swap=0 SHA256 sees bytes b6 b2 06 1d = LE of 0x1d06b2b6)
        internal_prevhash = bytes.fromhex(job.gbt["previousblockhash"])[::-1]
        prevhash_le = swab32(internal_prevhash).hex()
        version_hex = struct.pack(">I", job.version).hex()
        ntime_hex = struct.pack(">I", job.ntime).hex()
        nbits_hex = job.nbits  # send as-is: le32dec + swap=0 yields correct LE nbits bytes

        await self.send({
            "id": None,
            "method": "mining.notify",
            "params": [
                job.job_id,
                prevhash_le,
                cb1,
                cb2,
                job.merkle_branch,
                version_hex,
                nbits_hex,
                ntime_hex,
                clean,  # clean_jobs
            ],
        })
        self._current_job = job
        log.debug(f"[{self.peer}] NOTIFY job={job.job_id} cb1={cb1[-16:]} en1={self.extranonce1.hex()} cb2={cb2[:16]}")

    async def send_new_work(self, clean: bool = True):
        """Fetch fresh GBT and send a new job to this miner."""
        if not self.authorized or not self.miner_address:
            return
        try:
            gbt = await self.manager.get_current_gbt()
            job = self.manager.build_job(gbt, self.miner_address, self.extranonce1)
            await self.notify(job, clean)
        except Exception as e:
            log.warning(f"[{self.peer}] send_new_work error: {e}")

    # ── Message handlers ──────────────────────────────────────────────────────

    async def handle_subscribe(self, req_id, params):
        self.subscribed = True
        extranonce2_size = self.cfg["extranonce2_size"]
        await self.respond(req_id, [
            [
                ["mining.set_difficulty", "1"],
                ["mining.notify", "1"],
            ],
            self.extranonce1.hex(),
            extranonce2_size,
        ])
        diff = self.cfg["share_difficulty"]
        await self.send({
            "id": None,
            "method": "mining.set_difficulty",
            "params": [diff],
        })

    async def handle_authorize(self, req_id, params):
        if not params:
            await self.respond(req_id, False, [20, "Missing username"])
            return
        username = params[0]
        witness_program = decode_grc_address(username)
        if witness_program is None:
            log.warning(f"[{self.peer}] Invalid GRC address: {username!r}")
            await self.respond(req_id, False, [24, "Invalid GRC address (expected grc1q...)"])
            return
        self.miner_address = username
        self.authorized = True
        log.info(f"[{self.peer}] Authorized miner: {username}")
        await self.respond(req_id, True)
        await self.send_new_work(clean=True)

    async def handle_submit(self, req_id, params):
        if not self.authorized or not self._current_job:
            await self.respond(req_id, False, [24, "Not authorized"])
            return
        try:
            _worker, job_id, extranonce2_hex, ntime_hex, nonce_hex = params[:5]
        except (ValueError, TypeError):
            await self.respond(req_id, False, [20, "Malformed submit"])
            return

        job = self.manager.get_job(job_id)
        if job is None:
            await self.respond(req_id, False, [21, "Job not found"])
            return

        try:
            extranonce2 = bytes.fromhex(extranonce2_hex)
            ntime = int(ntime_hex, 16)
            bytes.fromhex(nonce_hex)  # validate hex
        except ValueError:
            await self.respond(req_id, False, [20, "Hex decode error"])
            return

        # Reconstruct coinbase (miner inserts en1 then en2 after coinbase1)
        coinbase_raw = job.coinbase1 + self.extranonce1 + extranonce2 + job.coinbase2
        coinbase_hash = sha256d(coinbase_raw)
        log.debug(f"[{self.peer}] FULLCB={coinbase_raw.hex()} hash={coinbase_hash.hex()[:16]}")

        # Compute merkle root
        merkle_root = coinbase_hash
        for branch_hash_hex in job.merkle_branch:
            branch_hash = bytes.fromhex(branch_hash_hex)[::-1]
            merkle_root = sha256d(merkle_root + branch_hash)

        # Build 80-byte block header using the correct Bitcoin wire format.
        # After notify pre-compensation, the miner's effective SHA256 operates on this
        # exact header. ntime is LE (standard Bitcoin), nonce is reversed because
        # cpuminer submits swab32(K) but SHA256 uses BE(K) = swab32(LE(K)).
        version_bytes = struct.pack("<I", job.version)
        prevhash_bytes = bytes.fromhex(job.gbt["previousblockhash"])[::-1]
        ntime_bytes = struct.pack("<I", ntime)
        nbits_bytes = bytes.fromhex(job.nbits)[::-1]   # LE uint32 for block header
        nonce_bytes = bytes.fromhex(nonce_hex)[::-1]
        header = (
            version_bytes +
            prevhash_bytes +
            merkle_root +
            ntime_bytes +
            nbits_bytes +
            nonce_bytes
        )
        log.debug(f"[{self.peer}] FULLHDR={header.hex()}")
        header_hash = sha256d(header)
        hash_int = int.from_bytes(header_hash[::-1], "big")

        # Check share difficulty
        share_target = diff_to_target(self.cfg["share_difficulty"])
        if hash_int > share_target:
            share_diff = (diff_to_target(1.0) / hash_int) if hash_int else 0
            log.warning(f"[{self.peer}] Low difficulty share: hash_diff={share_diff:.6f} target_diff={self.cfg['share_difficulty']} hash={header_hash[::-1].hex()[:16]}...")
            await self.respond(req_id, False, [23, "Low difficulty share"])
            return

        # Valid share — check if it also meets network difficulty
        if hash_int <= job.network_target:
            log.info(f"*** BLOCK FOUND by {self.miner_address} at height {job.height} ***")
            try:
                block_hex = self._serialize_block(header, coinbase_raw, job)
                result = self.manager.rpc.call("submitblock", [block_hex])
                if result is None or result == "":
                    log.info(f"Block accepted! height={job.height}")
                else:
                    log.warning(f"submitblock returned: {result}")
            except Exception as e:
                log.error(f"submitblock error: {e}")

        # Track share for hashrate estimation
        diff = self.cfg["share_difficulty"]
        self._share_log.append((time.time(), diff))
        # Keep only last 10 minutes
        cutoff = time.time() - 600
        self._share_log = [(t, d) for t, d in self._share_log if t >= cutoff]
        self.shares_accepted += 1

        await self.respond(req_id, True)

    def hashrate_khs(self) -> float:
        """Estimate hashrate from shares in the last 5 minutes."""
        now = time.time()
        window = 300
        recent = [(t, d) for t, d in self._share_log if now - t <= window]
        if len(recent) < 2:
            return 0.0
        elapsed = now - recent[0][0]
        if elapsed < 1:
            return 0.0
        total_hashes = sum(d * (2 ** 32) for _, d in recent)
        return total_hashes / elapsed / 1000  # KH/s

    def last_share_age(self) -> Optional[float]:
        if not self._share_log:
            return None
        return time.time() - self._share_log[-1][0]

    def to_stats(self) -> dict:
        return {
            "address": self.miner_address or "(pending)",
            "peer": str(self.peer),
            "connected_for": int(time.time() - self.connect_time),
            "shares_accepted": self.shares_accepted,
            "shares_rejected": self.shares_rejected,
            "hashrate_khs": round(self.hashrate_khs(), 2),
            "last_share_age": round(self.last_share_age(), 1) if self.last_share_age() is not None else None,
        }

    def _serialize_block(self, header: bytes, coinbase_raw: bytes, job: Job) -> str:
        txs = job.gbt.get("transactions", [])
        tx_count = 1 + len(txs)
        block = header + varint(tx_count) + coinbase_raw
        for tx in txs:
            block += bytes.fromhex(tx["data"])
        return block.hex()

    # ── Main session loop ─────────────────────────────────────────────────────

    async def run(self):
        self.manager.register_session(self)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(self.reader.readline(), timeout=600)
                except asyncio.TimeoutError:
                    log.info(f"[{self.peer}] Timeout, closing")
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                method = msg.get("method")
                req_id = msg.get("id")
                params = msg.get("params", [])
                if method == "mining.subscribe":
                    await self.handle_subscribe(req_id, params)
                elif method == "mining.authorize":
                    await self.handle_authorize(req_id, params)
                elif method == "mining.submit":
                    log.debug(f"[{self.peer}] SUBMIT params={params}")
                    await self.handle_submit(req_id, params)
                elif method == "mining.extranonce.subscribe":
                    await self.respond(req_id, True)
                else:
                    log.debug(f"[{self.peer}] Unknown method: {method}")
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            log.warning(f"[{self.peer}] Session error: {e}")
        finally:
            self.manager.unregister_session(self)
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            log.info(f"[{self.peer}] Disconnected")

# ── Server entry point ────────────────────────────────────────────────────────

async def main(cfg: dict):
    rpc = RPCClient(cfg["rpc_host"], cfg["rpc_port"], cfg["rpc_user"], cfg["rpc_pass"])

    # Test RPC connection
    try:
        info = rpc.call("getblockchaininfo")
        log.info(f"Connected to gregcoind: height={info['blocks']} chain={info['chain']}")
    except Exception as e:
        log.error(f"Cannot connect to gregcoind: {e}")
        sys.exit(1)

    manager = JobManager(rpc, cfg)

    # Start GBT poll loop
    asyncio.ensure_future(manager.poll_loop())

    async def client_connected(reader, writer):
        session = StratumSession(reader, writer, manager, cfg)
        await session.run()

    async def stats_connected(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), timeout=5)
            data = json.dumps(manager.get_stats()).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n" + data
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    host = cfg["stratum_host"]
    port = cfg["stratum_port"]
    server = await asyncio.start_server(client_connected, host, port)
    stats_server = await asyncio.start_server(stats_connected, "127.0.0.1", port + 1)
    log.info(f"Stratum pool listening on {host}:{port}")
    log.info(f"Stats API on 127.0.0.1:{port + 1}")
    log.info(f"Pool tag: {cfg.get('pool_tag', '/GRCPool/')}")
    log.info(f"Share difficulty: {cfg['share_difficulty']}")
    log.info("Miners connect with: -u <grc1q_address> -p x")

    async with server, stats_server:
        await server.serve_forever()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gregcoin stratum pool server")
    parser.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "grc-pool.conf"))
    args = parser.parse_args()
    cfg = load_config(args.config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown():
        log.info("Shutting down...")
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    try:
        loop.run_until_complete(main(cfg))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
