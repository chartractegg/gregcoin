# Gregcoin (GRC)

A peer-to-peer electronic cash system forked from Bitcoin Core, with modified chain parameters for experimental use.

## Parameters

| Parameter | Value |
|-----------|-------|
| Ticker | GRC |
| Total supply | 42,000,000 GRC |
| Block reward | 100 GRC (halving every 210,000 blocks) |
| Block time | 2.5 minutes |
| Address prefix | G (version byte 38) |
| Mainnet port | 8444 |
| RPC port | 8445 |
| Network magic | `0xd7 0xc6 0xb5 0xa4` |
| Genesis message | *The Times 04/Mar/2026 Trump: Starmer is no Churchill as Navy deploys to Cyprus* |

## Building

### Dependencies (Debian/Ubuntu)

```sh
sudo apt install build-essential cmake libboost-all-dev libssl-dev \
  libevent-dev libdb-dev libdb++-dev libminiupnpc-dev libzmq3-dev pkg-config
```

> **Debian 13 (trixie) note:** `libevent-dev` alone is not enough at runtime. Also install:
> ```sh
> sudo apt install libevent-core-2.1-7t64 libevent-extra-2.1-7t64 libevent-pthreads-2.1-7t64
> ```

### Compile

```sh
cmake -B build
cmake --build build -j$(nproc)
```

Binaries will be in `build/bin/`:
- `gregcoind` — full node daemon
- `gregcoin-cli` — RPC client

## Running a Full Node

### Configuration

Create `~/.gregcoin/gregcoin.conf`:

```ini
server=1
listen=1
rpcuser=grcuser
rpcpassword=yourpassword
rpcport=8445
port=8444
maxconnections=50
```

### Start the daemon

```sh
./build/bin/gregcoind -daemon -datadir=~/.gregcoin
./build/bin/gregcoin-cli -datadir=~/.gregcoin getblockchaininfo
```

### Connecting to the network

A public seed node is being set up at `coin.gregcathcart.com:8444`. Once available, add it as a peer:

```sh
./build/bin/gregcoin-cli -datadir=~/.gregcoin addnode "coin.gregcathcart.com:8444" "add"
```

Check your connections:

```sh
./build/bin/gregcoin-cli -datadir=~/.gregcoin getpeerinfo
```

## Mining

Gregcoin uses SHA-256 proof of work and is intentionally CPU-minable (low difficulty, no ASICs).

GUI miners for macOS, Windows, and Linux are in [`tools/grc-miner-gui/`](tools/grc-miner-gui/):

| Version | Directory | How to run |
|---------|-----------|------------|
| Python/tkinter | `tkinter/` | `python3 grc_miner.py` |
| Electron | `electron/` | `npm install && npm start` |
| Go+Fyne | `go-fyne/` | `go run .` |

All GUI miners connect to a local or remote `gregcoind` node via JSON-RPC and use `getblocktemplate`.

To mine against a running node:
1. Start `gregcoind` and wait for it to sync
2. Create a wallet and get an address: `gregcoin-cli -datadir=~/.gregcoin getnewaddress`
3. Open a GUI miner and point it at `127.0.0.1:8445` with your RPC credentials

## Development / Regtest

```sh
./build/bin/gregcoind -regtest -daemon -datadir=~/.gregcoin-regtest
./build/bin/gregcoin-cli -regtest -datadir=~/.gregcoin-regtest createwallet mywallet
./build/bin/gregcoin-cli -regtest -datadir=~/.gregcoin-regtest getnewaddress
./build/bin/gregcoin-cli -regtest -datadir=~/.gregcoin-regtest generatetoaddress 10 <address>
# 10 blocks × 100 GRC = 1000 GRC (subject to coinbase maturity)
```

## Key Modified Files

For those curious about the fork changes:

| File | Change |
|------|--------|
| `CMakeLists.txt` | Binary/project name |
| `src/chainparamsbase.cpp` | Port defaults (8444/8445) |
| `src/chainparamsseeds.h` | DNS/fixed seeds removed |
| `src/consensus/amount.h` | Supply and coin constants |
| `src/kernel/chainparams.cpp` | Chain params, ports, magic bytes, genesis block |
| `src/validation.cpp` | Block time tweaks |

## License

Released under the MIT license. See [COPYING](COPYING).
