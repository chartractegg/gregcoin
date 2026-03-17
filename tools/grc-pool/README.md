# Gregcoin Public Pool

Gregcoin (GRC) is a custom Bitcoin fork. This is a **solo pool** — when you find a block, **100% of the 100 GRC block reward goes directly to your wallet address**. No fees, no pool account, no sign-up.

The pool runs as a Tor hidden service. Your home IP is never exposed to the pool operator or other miners.

---

## Onion Addresses

| Service | Address | Purpose |
|---------|---------|---------|
| **Mining pool** | `tdeva2kqkihornna6fhon5cddrevqwwndge46qglbh45f3fzgoux7vyd.onion:3333` | stratum+tcp, connect your miner here |
| **P2P node** | `mhkm2jaynobkxrragdq7ntz3gypbbn7lcsybwoergjyjp57ohv6cv7id.onion:8444` | add this node to sync the blockchain |

---

## Prerequisites

### 1. Install Tor
You need Tor running locally to connect to `.onion` services.

**Linux (Debian/Ubuntu):**
```bash
sudo apt install tor
sudo systemctl start tor
```

**macOS (Homebrew):**
```bash
brew install tor
brew services start tor
```

**Windows:** Download the [Tor Expert Bundle](https://www.torproject.org/download/tor/) and run `tor.exe`.

Tor's SOCKS5 proxy listens on `127.0.0.1:9050` by default.

### 2. Get a GRC Wallet Address
You need a `grc1q...` address to receive block rewards.

**Option A:** Use one of the GUI miners from this repo — they generate a wallet automatically on first run.

**Option B:** Run your own gregcoind node:
```bash
./gregcoind -daemon -datadir=~/.gregcoin
./gregcoin-cli -datadir=~/.gregcoin getnewaddress
```

**Option C:** Get gregcoind/gregcoin-cli from [git.gregcathcart.com/admin/gregcoin](https://git.gregcathcart.com/admin/gregcoin).

---

## Mining with cpuminer-multi

[cpuminer-multi](https://github.com/tpruvot/cpuminer-multi) is the recommended standard CPU miner.

```bash
./cpuminer \
  -a sha256d \
  -o stratum+tcp://tdeva2kqkihornna6fhon5cddrevqwwndge46qglbh45f3fzgoux7vyd.onion:3333 \
  -u grc1q<YOUR_ADDRESS_HERE> \
  -p x \
  --proxy=socks5://127.0.0.1:9050
```

Replace `grc1q<YOUR_ADDRESS_HERE>` with your actual GRC wallet address.

---

## Mining with gregminer

gregminer is the custom Gregcoin miner (faster for this network):

```bash
./gregminer \
  -a sha256d \
  -o stratum+tcp://tdeva2kqkihornna6fhon5cddrevqwwndge46qglbh45f3fzgoux7vyd.onion:3333 \
  -u grc1q<YOUR_ADDRESS_HERE> \
  -p x \
  -x socks5://127.0.0.1:9050
```

---

## Running Your Own Full Node (Optional)

If you want to verify blocks yourself instead of trusting the pool, run your own gregcoind and add the P2P onion peer:

In your `~/.gregcoin/gregcoin.conf`:
```
onion=127.0.0.1:9050
addnode=mhkm2jaynobkxrragdq7ntz3gypbbn7lcsybwoergjyjp57ohv6cv7id.onion:8444
```

The blockchain will sync over Tor. Once synced, you can mine locally with getblocktemplate mode.

---

## Chain Parameters

| Parameter | Value |
|-----------|-------|
| Symbol | GRC |
| Algorithm | SHA256d |
| Block reward | 100 GRC |
| Block time | 2.5 minutes |
| Max supply | 42,000,000 GRC |
| P2P port | 8444 |
| RPC port | 8445 |

---

## FAQ

**Q: How does the solo pool work?**
When you connect, the pool gives you a block template where the coinbase transaction pays to **your address**. If your miner finds a valid block, you submit it through the pool and the reward goes directly to you. The pool operator never touches your coins.

**Q: Why use Tor?**
The pool operator doesn't want to expose their home IP address, and neither do you. All connections are `.onion`-to-`.onion`.

**Q: Is there a pool fee?**
No. Solo pool, zero fee.

**Q: What hash rate do I need?**
The network hash rate is small. A single modern CPU can meaningfully contribute. The more CPU threads, the better.
