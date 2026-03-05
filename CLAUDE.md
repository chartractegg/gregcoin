# Gregcoin (GRC) — CLAUDE.md

## Project Overview

Gregcoin is a fork of Bitcoin Core. The goal is a custom cryptocurrency with modified parameters.

## Key Parameters

| Parameter | Value |
|-----------|-------|
| Ticker | GRC |
| Total supply | 42,000,000 GRC |
| Block reward | 100 GRC (halving every 210,000 blocks) |
| Block time | 2.5 minutes (150 seconds) |
| Address prefix | G (version byte 38) |
| Mainnet port | 8444 |
| RPC port | 8445 |
| Network magic | 0xd7 0xc6 0xb5 0xa4 |

## Key Modified Files

- `CMakeLists.txt` — binary/project name
- `src/chainparamsbase.cpp` — port defaults
- `src/chainparamsseeds.h` — seeds removed
- `src/consensus/amount.h` — supply/coin constants
- `src/kernel/chainparams.cpp` — chain parameters, ports, magic, genesis blocks
- `src/validation.cpp` — block time / consensus tweaks

## Build

Standard Bitcoin Core cmake build:

```sh
cmake -B build
cmake --build build -j$(nproc)
```

## Testing

Run regtest to verify mining rewards:

```sh
./build/src/bitcoind -regtest -daemon
./build/src/bitcoin-cli -regtest generatetoaddress 10 <address>
# Expect ~1000 GRC across 10 blocks
```

## Genesis Block (Mainnet)

Mined 2026-03-04 in 1.5s using `tools/genesis_miner/mine_genesis.py`.

| Field | Value |
|-------|-------|
| nNonce | 637316 |
| nTime | 1741046400 |
| nBits | 0x1e0ffff0 |
| Hash | `00000051a1a941989c60b71a70412ec239f4c968d2c8ad5a34a5eb4e7bc68775` |
| MerkleRoot | `1c36738b95ca56a0cdfde1b809ded354bf8d68a09a118d2fd0e3d0fcf0d6399d` |

## GUI Miners

Three cross-platform miners in `tools/grc-miner-gui/`:

| Version | Dir | Run |
|---------|-----|-----|
| Python/tkinter | `tkinter/` | `python3 grc_miner.py` |
| Electron | `electron/` | `npm install && npm start` |
| Go+Fyne | `go-fyne/` | `go run .` |

All connect to `bitcoind` via JSON-RPC and mine using `getblocktemplate`.

## Pi Cluster Nodes

| Hostname | IP | Notes |
|----------|----|-------|
| picard | 10.0.1.220 | control plane, seed node |
| data | 10.0.1.222 | worker |
| troi | 10.0.1.219 | worker (Pi 4) |
| worf | 10.0.1.224 | worker |

Connect peers with: `bitcoin-cli addnode "IP:8444" "add"`

## Building

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
```

Build started 2026-03-04. Binaries in `build/src/`.

## Notes

- DNS seeds and fixed seeds removed — peers added manually
- powLimit changed to `00000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` (Litecoin-style, CPU-minable)
- Initial block difficulty: ~1M hashes per block (~20s at 48 KH/s on 4 Pi 5s)
- Based on Bitcoin Core master as of 2026-03-04
