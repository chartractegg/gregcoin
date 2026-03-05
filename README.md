# Gregcoin (GRC)

A peer-to-peer electronic cash system forked from Bitcoin Core.

## Parameters

| Parameter | Value |
|-----------|-------|
| Ticker | GRC |
| Total supply | 42,000,000 GRC |
| Block reward | 100 GRC (halving every 210,000 blocks) |
| Block time | 2.5 minutes (150 seconds) |
| Address prefix | G (version byte 38) |
| Mainnet port | 8444 |
| RPC port | 8445 |
| Network magic | `0xd7 0xc6 0xb5 0xa4` |
| Genesis message | *The Times 04/Mar/2026 Trump: Starmer is no Churchill as Navy deploys to Cyprus* |

## Building

### Dependencies (Debian/Ubuntu)

```sh
sudo apt install build-essential cmake libboost-all-dev libssl-dev libevent-dev \
  libdb-dev libdb++-dev libminiupnpc-dev libzmq3-dev pkg-config
```

### Compile

```sh
cmake -B build
cmake --build build -j$(nproc)
```

Binaries will be in `build/src/`:
- `bitcoind` — full node daemon
- `bitcoin-cli` — RPC client
- `bitcoin-qt` — GUI wallet (if Qt is available)

## Running

### Mainnet

```sh
./build/src/bitcoind -daemon
./build/src/bitcoin-cli getblockchaininfo
```

Configuration file: `~/.bitcoin/bitcoin.conf`

```ini
server=1
rpcuser=grcuser
rpcpassword=yourpassword
rpcport=8445
port=8444
```

### Regtest (development)

```sh
./build/src/bitcoind -regtest -daemon
./build/src/bitcoin-cli -regtest createwallet mywallet
./build/src/bitcoin-cli -regtest getnewaddress
./build/src/bitcoin-cli -regtest generatetoaddress 10 <address>
# Each block earns 100 GRC; 10 blocks = 1000 GRC (minus maturity delay)
```

## Mining

See [`tools/grc-miner-gui/`](tools/grc-miner-gui/) for GUI miners available for:
- macOS
- Windows
- Linux

Three implementations are provided: Python/tkinter, Electron, and Go+Fyne.

### Genesis block

The genesis block must be mined once before the mainnet can be launched.

```sh
cd tools/genesis_miner
python3 mine_genesis.py
```

To parallelise across the Pi cluster:

```sh
bash mine_genesis_parallel.sh
```

## Network

No DNS seeds or fixed seeds are configured. Nodes must be added manually:

```sh
./build/src/bitcoin-cli addnode "10.0.1.220:8444" "add"
./build/src/bitcoin-cli addnode "10.0.1.222:8444" "add"
./build/src/bitcoin-cli addnode "10.0.1.219:8444" "add"
./build/src/bitcoin-cli addnode "10.0.1.224:8444" "add"
```

## Pi Cluster Nodes

| Hostname | IP | Role |
|----------|----|------|
| picard | 10.0.1.220 | seed node |
| data | 10.0.1.222 | miner |
| troi | 10.0.1.219 | miner |
| worf | 10.0.1.224 | miner |

## License

Released under the MIT license. See [COPYING](COPYING).
