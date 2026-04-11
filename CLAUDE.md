# Gregcoin (GRC) — CLAUDE.md

## Project Overview

Gregcoin is a fork of Bitcoin Core. Custom cryptocurrency running on a Raspberry Pi cluster.

## Key Parameters

| Parameter | Value |
|-----------|-------|
| Ticker | GRC |
| Total supply | 42,000,000 GRC |
| Block reward | 100 GRC (halving every 210,000 blocks) |
| Block time | 2.5 minutes (150 seconds) |
| Address prefix | bech32 `grc1q...` (segwit) |
| Mainnet P2P port | 8444 |
| RPC port | 8445 |
| Network magic | 0xd7 0xc6 0xb5 0xa4 |

## Key Modified Files

- `CMakeLists.txt` — binary/project name (`gregcoind`, `gregcoin-cli`, etc.)
- `src/chainparamsbase.cpp` — port defaults
- `src/chainparamsseeds.h` — DNS/fixed seeds removed
- `src/consensus/amount.h` — supply/coin constants
- `src/kernel/chainparams.cpp` — chain parameters, ports, magic, genesis blocks
- `src/validation.cpp` — block time / consensus tweaks

## Build

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
# Binaries in build/bin/
```

## Testing

Run regtest to verify mining rewards:

```sh
./build/bin/gregcoind -regtest -daemon
./build/bin/gregcoin-cli -regtest generatetoaddress 10 <address>
# Expect ~1000 GRC across 10 blocks (coinbase matures after 100 blocks)
```

## Genesis Block (Mainnet — chain v2, reset 2026-03-12)

Mined using `tools/genesis_miner/mine_genesis.py`.

| Field | Value |
|-------|-------|
| nNonce | 17937210 |
| nTime | 1773273600 (2026-03-12 00:00:00 UTC) |
| nBits | 0x1e00227b |
| Hash | `0000001cfd5f2ca55e9815affb599b45501e492fa232fd3917d6490981f9e00d` |
| MerkleRoot | `1c36738b95ca56a0cdfde1b809ded354bf8d68a09a118d2fd0e3d0fcf0d6399d` |

**Why reset:** Original chain had genesis timestamp 2025-03-04 (one year early), causing broken difficulty retargeting. Reset with correct 2026-03-12 timestamp and harder nBits.

## Pi Cluster Nodes

| Hostname | IP | Role |
|----------|----|------|
| picard | 10.0.1.220 | control plane — gregcoind, pool server |
| riker | 10.0.1.221 | worker — gregminer |
| data | 10.0.1.222 | worker — gregminer |
| laforge | 10.0.1.223 | worker — gregminer |
| worf | 10.0.1.224 | worker — gregminer |
| lore | 10.0.1.218 | worker — gregminer |
| troi | 10.0.1.219 | worker — gregminer (Pi 4) |

Connect peers: `gregcoin-cli addnode "IP:8444" "add"`

## Mining Pool (grc-pool)

Solo stratum pool in `tools/grc-pool/grc-pool.py`. Each miner's found block pays 100% to their address.

- Config template: `tools/grc-pool/grc-pool.conf.example` (actual conf is gitignored — contains RPC credentials)
- Stratum port: 3333
- Stats API: `http://127.0.0.1:3334/stats`
- Systemd: `systemctl --user start grc-pool`

### cpuminer Byte-Order Pre-Compensation

cpuminer-multi (including the custom `gregminer` binary) uses `sha256_transform(swap=0)` on LE (ARM) machines. Each 32-bit word in `work->data` is passed to SHA256 as-is, meaning SHA256 sees the big-endian byte representation of each stored uint32. This requires the pool to pre-compensate the stratum `mining.notify` params so the miner's SHA256 processes the correct Bitcoin wire-format bytes:

| Field | GBT value | Notify (sent to miner) | handle_submit reconstruction |
|-------|-----------|----------------------|------------------------------|
| version | `536870912` (0x20000000) | BE hex `"20000000"` | `struct.pack("<I", version)` |
| prevhash | display hash | `swab32(internal_bytes).hex()` | `bytes.fromhex(display)[::-1]` |
| ntime | uint32 timestamp | BE hex | `struct.pack("<I", ntime)` |
| nbits | `"1d06b2b6"` | as-is `"1d06b2b6"` | `bytes.fromhex(nbits)[::-1]` |
| nonce | — | — | `bytes.fromhex(submitted)[::-1]` |

**Merkle root** is computed via standard `sha256d` (identical to Python's `hashlib`) — no byte-swap compensation needed.

**Key insight for nbits:** `le32dec("1d06b2b6")` = `0xb6b2061d`; swap=0 SHA256 sees bytes `b6 b2 06 1d` = correct LE representation of `0x1d06b2b6`. So notify sends nbits as-is from GBT, but handle_submit reverses it for the block header (`[::-1]`).

**RPC Python gotcha:** Do NOT include `"jsonrpc"` field in RPC calls — bitcoind returns HTTP 400. Use `{"method": ..., "params": ..., "id": 1}` only.

## Mining Tools

| Path | Purpose |
|------|---------|
| `tools/miner-control.sh` | Start/stop/deploy gregminer across cluster |
| `tools/grc-dashboard/grc-dashboard.py` | Live TUI dashboard (chain, txs, mining stats, pool) |
| `tools/grc-pool/grc-pool.py` | Asyncio stratum solo pool server |
| `tools/grc-pool/grc-pool.conf.example` | Pool config template |
| `tools/grc-pool/README.md` | Public mining instructions |
| `tools/genesis_miner/mine_genesis.py` | Mine a new genesis block |
| `tools/grc-miner-gui/tkinter/grc_miner.py` | Tkinter GUI miner |
| `tools/grc-miner-gui/go-fyne/` | Go+Fyne GUI miner+wallet |

## miner-control.sh

```sh
tools/miner-control.sh [deploy|start|stop|restart|status] [--cpu 25|50|75|100]
```

`--cpu` sets thread count per node: 25%=1, 50%=2, 75%=3, 100%=4

## Notes

- DNS seeds and fixed seeds removed — peers added manually or via Tor
- powLimit: `00000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` (Litecoin-style, CPU-minable)
- Retarget interval: 576 blocks (~1 day at target rate), max 4× adjustment per period
- Tor hidden services configured in `/etc/tor/torrc` (manual, not `listenonion=1` — avoids port collision with RPC on 8445)
- Based on Bitcoin Core master as of 2026-03-04
