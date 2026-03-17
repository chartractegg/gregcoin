# Gregcoin (GRC)

A peer-to-peer electronic cash system forked from Bitcoin Core, with modified chain parameters for experimental use on a Raspberry Pi cluster.

## Chain Parameters

| Parameter | Value |
|-----------|-------|
| Ticker | GRC |
| Total supply | 42,000,000 GRC |
| Block reward | 100 GRC (halving every 210,000 blocks) |
| Block time | 2.5 minutes (150 seconds) |
| Address format | Bech32 — `grc1q...` |
| Mainnet P2P port | 8444 |
| RPC port | 8445 |
| Network magic | `0xd7 0xc6 0xb5 0xa4` |
| Proof of work | SHA-256d (CPU-minable) |
| Genesis message | *The Times 04/Mar/2026 Trump: Starmer is no Churchill as Navy deploys to Cyprus* |

## Building

### Dependencies (Debian/Ubuntu)

```sh
sudo apt install build-essential cmake libboost-all-dev libssl-dev \
  libevent-dev libdb-dev libdb++-dev libminiupnpc-dev libzmq3-dev pkg-config
```

> **Debian 13 (trixie) note:** Also install the libevent runtime libs:
> ```sh
> sudo apt install libevent-core-2.1-7t64 libevent-extra-2.1-7t64 libevent-pthreads-2.1-7t64
> ```

### Compile

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release
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
maxconnections=50
listenonion=0
```

> **Tor note:** Do NOT use `listenonion=1` — it would try to bind port 8445 (P2P+1), which conflicts with the RPC port. Use the manual Tor config below instead.

### Start the daemon

```sh
./build/bin/gregcoind -daemon -datadir=~/.gregcoin
./build/bin/gregcoin-cli -datadir=~/.gregcoin getblockchaininfo
```

### Connect to the network via Tor

The seed node is reachable as a Tor hidden service. Install Tor and add to `gregcoin.conf`:

```ini
onion=127.0.0.1:9050
addnode=mhkm2jaynobkxrragdq7ntz3gypbbn7lcsybwoergjyjp57ohv6cv7id.onion:8444
```

Then restart the daemon and verify connections:

```sh
./build/bin/gregcoin-cli -datadir=~/.gregcoin getpeerinfo
```

## Mining

Gregcoin uses SHA-256d proof of work and is intentionally CPU-minable. Difficulty auto-adjusts to target 2.5-minute block times.

### Get a GRC address

First, create a wallet and get an address to receive rewards:

```sh
./build/bin/gregcoin-cli -datadir=~/.gregcoin createwallet mywallet
./build/bin/gregcoin-cli -datadir=~/.gregcoin getnewaddress
# Returns something like: grc1qthh3zwq09k22yqegv7265xgfvzx447y3rwf3a0
```

### Option 1 — Solo pool (recommended for external miners)

A public Tor-accessible stratum pool runs on the network. It is **solo-style**: when your miner finds a block, 100% of the 100 GRC reward goes directly to your address. No registration, no fees, no payout delays.

Connect using [cpuminer-multi](https://github.com/tpruvot/cpuminer-multi) via Tor SOCKS5:

```sh
cpuminer-multi -a sha256d \
  -o stratum+tcp://tdeva2kqkihornna6fhon5cddrevqwwndge46qglbh45f3fzgoux7vyd.onion:3333 \
  -u grc1q<your_address> -p x \
  --proxy=socks5://127.0.0.1:9050
```

See [`tools/grc-pool/README.md`](tools/grc-pool/README.md) for full setup instructions.

### Option 2 — Direct GBT mining (local node required)

If you run your own `gregcoind` node, you can mine directly against it:

```sh
cpuminer-multi -a sha256d \
  -o http://grcuser:yourpassword@127.0.0.1:8445 \
  --coinbase-addr=grc1q<your_address>
```

### Option 3 — GUI miners

Cross-platform GUI miners are in [`tools/grc-miner-gui/`](tools/grc-miner-gui/):

| Version | Directory | How to run |
|---------|-----------|------------|
| Python/tkinter | `tkinter/` | `python3 grc_miner.py` |
| Electron | `electron/` | `npm install && npm start` |
| Go+Fyne | `go-fyne/` | `go run .` (Windows/Linux/macOS) |

All GUI miners connect to a local `gregcoind` via JSON-RPC and mine using `getblocktemplate`.

## Tools

| Tool | Description |
|------|-------------|
| [`tools/grc-dashboard/grc-dashboard.py`](tools/grc-dashboard/grc-dashboard.py) | htop-style terminal dashboard — chain stats, per-node hashrates, transaction history, connected pool miners |
| [`tools/grc-pool/grc-pool.py`](tools/grc-pool/grc-pool.py) | Asyncio stratum solo pool server — each miner's coinbase pays to their own address |
| [`tools/miner-control.sh`](tools/miner-control.sh) | Deploy and control miners across a cluster of nodes over SSH |
| [`tools/genesis_miner/mine_genesis.py`](tools/genesis_miner/mine_genesis.py) | Mine a new genesis block with custom parameters |

### Dashboard

```sh
python3 tools/grc-dashboard/grc-dashboard.py
```

Keys: `F1` Overview · `F2` Transactions · `F3` Mining Stats · `F4`/`P` Pool Miners · `s` Start · `x` Stop · `r` Restart · `q` Quit

## Development / Regtest

```sh
./build/bin/gregcoind -regtest -daemon -datadir=~/.gregcoin-regtest
./build/bin/gregcoin-cli -regtest -datadir=~/.gregcoin-regtest createwallet mywallet
./build/bin/gregcoin-cli -regtest -datadir=~/.gregcoin-regtest getnewaddress
./build/bin/gregcoin-cli -regtest -datadir=~/.gregcoin-regtest generatetoaddress 10 <address>
# 10 blocks × 100 GRC = 1000 GRC (subject to 100-block coinbase maturity)
```

## Key Modified Files

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
