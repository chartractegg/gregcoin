#!/usr/bin/env python3
"""
Gregcoin Genesis Block Miner
Finds a valid nNonce for the Gregcoin mainnet genesis block.

Usage:
  python3 mine_genesis.py [start_nonce [end_nonce]]

Defaults: start=0, end=0xFFFFFFFF
Runs all nonces in the given range searching for a hash that satisfies nBits.
"""
import hashlib
import struct
import sys
import time

# ── Genesis parameters (must match chainparams.cpp) ───────────────────────────
TIMESTAMP_MSG = b"The Times 04/Mar/2026 Trump: Starmer is no Churchill as Navy deploys to Cyprus"
GENESIS_PUBKEY = bytes.fromhex(
    "04678afdb0fe5548271967f1a67130b7105cd6a828e03909a67962e0ea1f61deb"
    "649f6bc3f4cef38c4f35504e51ec112de5c384df7ba0b8d578a4c702b6bf11d5f"
)
GENESIS_REWARD = 100 * 100_000_000   # 100 GRC in satoshis
N_TIME  = 1741046400                 # 2026-03-04 00:00:00 UTC
N_BITS  = 0x1e0ffff0
VERSION = 1
# ──────────────────────────────────────────────────────────────────────────────


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def varint(n: int) -> bytes:
    if n < 0xfd:
        return struct.pack('<B', n)
    elif n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    elif n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    else:
        return b'\xff' + struct.pack('<Q', n)


def script_push(data: bytes) -> bytes:
    """Minimal CScript push for a byte vector."""
    n = len(data)
    if n < 0x4c:          # OP_PUSHDATA1 threshold
        return struct.pack('<B', n) + data
    elif n <= 0xff:
        return b'\x4c' + struct.pack('<B', n) + data
    elif n <= 0xffff:
        return b'\x4d' + struct.pack('<H', n) + data
    else:
        return b'\x4e' + struct.pack('<I', n) + data


def script_push_int(value: int) -> bytes:
    """CScript << int64: minimal serialisation of a script number."""
    if value == 0:
        return b'\x00'
    negative = value < 0
    absval = abs(value)
    result = []
    while absval:
        result.append(absval & 0xff)
        absval >>= 8
    if result[-1] & 0x80:
        result.append(0x80 if negative else 0x00)
    elif negative:
        result[-1] |= 0x80
    data = bytes(result)
    return struct.pack('<B', len(data)) + data


def build_coinbase_tx() -> bytes:
    """
    Replicates chainparams.cpp CreateGenesisBlock coinbase transaction:
      vin[0].scriptSig = CScript() << 486604799 << CScriptNum(4) << pszTimestamp
      vout[0].nValue   = genesisReward
      vout[0].scriptPubKey = CScript() << pubkey << OP_CHECKSIG
    """
    # scriptSig: << 486604799 (= 0x1d00ffff) << 4 << TIMESTAMP_MSG
    script_sig = (
        script_push_int(486604799) +
        script_push_int(4) +
        script_push(TIMESTAMP_MSG)
    )

    # scriptPubKey: OP_DATA_65 <pubkey> OP_CHECKSIG
    script_pubkey = script_push(GENESIS_PUBKEY) + b'\xac'

    tx = (
        struct.pack('<i', 1) +          # version
        varint(1) +                      # vin count
        b'\x00' * 32 +                  # prevout hash (null)
        struct.pack('<I', 0xffffffff) + # prevout index
        varint(len(script_sig)) +
        script_sig +
        struct.pack('<I', 0xffffffff) + # sequence
        varint(1) +                      # vout count
        struct.pack('<q', GENESIS_REWARD) +
        varint(len(script_pubkey)) +
        script_pubkey +
        struct.pack('<I', 0)             # locktime
    )
    return tx


def bits_to_target(nbits: int) -> bytes:
    """Convert compact nBits to a 32-byte big-endian target."""
    exponent = (nbits >> 24) & 0xff
    mantissa = nbits & 0x007fffff
    # target = mantissa * 256^(exponent-3)
    target_int = mantissa * (256 ** (exponent - 3))
    return target_int.to_bytes(32, 'big')


def hash_less_than_target(h: bytes, target: bytes) -> bool:
    return h < target


def mine(start_nonce: int = 0, end_nonce: int = 0xffffffff) -> None:
    # Build coinbase tx once
    coinbase_tx = build_coinbase_tx()
    txid = sha256d(coinbase_tx)        # Only tx → merkle root = txid
    merkle_root = txid                 # Little-endian as stored in Bitcoin

    target = bits_to_target(N_BITS)

    # Block header template (first 76 bytes fixed; last 4 = nNonce)
    header_prefix = (
        struct.pack('<i', VERSION) +          # version
        b'\x00' * 32 +                        # hashPrevBlock (null for genesis)
        merkle_root +                          # hashMerkleRoot
        struct.pack('<I', N_TIME) +            # nTime
        struct.pack('<I', N_BITS)              # nBits
    )
    assert len(header_prefix) == 76

    print(f"Gregcoin Genesis Miner")
    print(f"  Timestamp : {TIMESTAMP_MSG.decode()}")
    print(f"  nTime     : {N_TIME} (0x{N_TIME:08x})")
    print(f"  nBits     : 0x{N_BITS:08x}")
    print(f"  Target    : {target.hex()}")
    print(f"  MerkleRoot: {merkle_root[::-1].hex()}")
    print(f"  Range     : {start_nonce} – {end_nonce}")
    print()

    t0 = time.time()
    report_every = 500_000
    nonce = start_nonce

    header = bytearray(header_prefix + b'\x00\x00\x00\x00')

    while nonce <= end_nonce:
        struct.pack_into('<I', header, 76, nonce)
        h = sha256d(bytes(header))
        if h[::-1] < target:
            elapsed = time.time() - t0
            block_hash = h[::-1].hex()
            print(f"  ✓ GENESIS FOUND!")
            print(f"  nNonce    : {nonce} (0x{nonce:08x})")
            print(f"  Hash      : {block_hash}")
            print(f"  Elapsed   : {elapsed:.1f}s  ({nonce / elapsed / 1e6:.3f} MH/s)")
            print()
            print("Update chainparams.cpp mainnet genesis with:")
            print(f'  genesis = CreateGenesisBlock({N_TIME}, {nonce}, 0x{N_BITS:08x}, 1, 100 * COIN);')
            print(f'  assert(consensus.hashGenesisBlock == uint256{{"{block_hash}"}});')
            print(f'  assert(genesis.hashMerkleRoot == uint256{{"{merkle_root[::-1].hex()}"}});')
            return

        nonce += 1
        if nonce % report_every == 0:
            elapsed = time.time() - t0
            rate = nonce / elapsed / 1e6 if elapsed > 0 else 0
            print(f"  {nonce:>12,}  elapsed={elapsed:.1f}s  rate={rate:.3f} MH/s", end='\r')

    print(f"\nNot found in range [{start_nonce}, {end_nonce}]")


if __name__ == '__main__':
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end   = int(sys.argv[2]) if len(sys.argv) > 2 else 0xffffffff
    mine(start, end)
