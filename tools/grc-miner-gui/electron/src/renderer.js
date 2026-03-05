'use strict';
const crypto = require('crypto');
const http   = require('http');

// ── SHA256d ───────────────────────────────────────────────────────────────────
function sha256d(buf) {
  return crypto.createHash('sha256').update(
    crypto.createHash('sha256').update(buf).digest()
  ).digest();
}

// ── Varint ────────────────────────────────────────────────────────────────────
function varint(n) {
  if (n < 0xfd) return Buffer.from([n]);
  if (n <= 0xffff) { const b = Buffer.alloc(3); b[0] = 0xfd; b.writeUInt16LE(n, 1); return b; }
  const b = Buffer.alloc(5); b[0] = 0xfe; b.writeUInt32LE(n, 1); return b;
}

// ── Merkle root ───────────────────────────────────────────────────────────────
function merkleRoot(txids) {
  if (!txids.length) return Buffer.alloc(32);
  let layer = txids.slice();
  while (layer.length > 1) {
    if (layer.length % 2) layer.push(layer[layer.length - 1]);
    const next = [];
    for (let i = 0; i < layer.length; i += 2)
      next.push(sha256d(Buffer.concat([layer[i], layer[i+1]])));
    layer = next;
  }
  return layer[0];
}

// ── nBits target ──────────────────────────────────────────────────────────────
function bitsToTargetBigInt(bitsHex) {
  const n = BigInt('0x' + bitsHex);
  const exp  = Number((n >> 24n) & 0xffn);
  const mant = n & 0x007fffffn;
  return mant * (256n ** BigInt(exp - 3));
}

// ── Build coinbase tx ─────────────────────────────────────────────────────────
function buildCoinbase(height, coinbaseValue, scriptPubKey, extraNonce) {
  const heightBuf = (() => {
    let v = height; const b = [];
    while (v > 0) { b.push(v & 0xff); v >>>= 8; }
    return Buffer.from(b);
  })();
  const enBuf = Buffer.alloc(4); enBuf.writeUInt32LE(extraNonce);
  const scriptSig = Buffer.concat([
    Buffer.from([heightBuf.length]), heightBuf,
    Buffer.from([4]), enBuf,
  ]);
  const ver   = Buffer.alloc(4); ver.writeInt32LE(1);
  const seq   = Buffer.from([0xff,0xff,0xff,0xff]);
  const val   = Buffer.alloc(8); val.writeBigInt64LE(BigInt(coinbaseValue));
  const lock  = Buffer.alloc(4);
  const prevH = Buffer.alloc(32);
  const prevI = Buffer.from([0xff,0xff,0xff,0xff]);
  return Buffer.concat([
    ver, varint(1),
    prevH, prevI, varint(scriptSig.length), scriptSig, seq,
    varint(1), val, varint(scriptPubKey.length), scriptPubKey, lock,
  ]);
}

// ── Base58 decode → hash160 → P2PKH scriptPubKey ─────────────────────────────
const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function p2pkhScript(address) {
  let n = 0n;
  for (const c of address) n = n * 58n + BigInt(B58.indexOf(c));
  const raw = Buffer.from(n.toString(16).padStart(50, '0'), 'hex');
  const h160 = raw.slice(1, 21);
  return Buffer.concat([Buffer.from([0x76,0xa9,0x14]), h160, Buffer.from([0x88,0xac])]);
}

// ── RPC ───────────────────────────────────────────────────────────────────────
function rpcCall(host, port, user, pass, method, params = []) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ jsonrpc:'1.1', id:1, method, params });
    const auth = Buffer.from(`${user}:${pass}`).toString('base64');
    const req  = http.request(
      { host, port, path:'/', method:'POST',
        headers:{ 'Content-Type':'application/json', Authorization:`Basic ${auth}` }
      },
      res => {
        let data = '';
        res.on('data', d => data += d);
        res.on('end', () => {
          try {
            const r = JSON.parse(data);
            if (r.error) reject(new Error(JSON.stringify(r.error)));
            else resolve(r.result);
          } catch(e) { reject(e); }
        });
      }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ── Mining state ──────────────────────────────────────────────────────────────
let mining = false;
let extraNonce = 0;
let blocksFound = 0;
let startTime = null;
let hashRate = 0;
let statInterval = null;
let miningTimeout = null;

// ── DOM refs ──────────────────────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const cfg = () => ({
  host: $('i-host').value.trim(),
  port: parseInt($('i-port').value),
  user: $('i-user').value.trim(),
  pass: $('i-pass').value,
  addr: $('i-addr').value.trim(),
});

function log(msg, err=false) {
  const d = $('log');
  const ts = new Date().toTimeString().slice(0,8);
  const line = document.createElement('div');
  line.className = 'log-line' + (err ? ' log-err' : '');
  line.textContent = `[${ts}] ${msg}`;
  d.appendChild(line);
  d.scrollTop = d.scrollHeight;
}

function setStatus(text, active) {
  $('status-text').textContent = text;
  $('dot').className = 'dot' + (active ? ' active' : '');
  $('prog-bar').style.display = active ? 'block' : 'none';
}

function fmtRate(r) {
  if (r >= 1e6) return `${(r/1e6).toFixed(2)} MH/s`;
  if (r >= 1e3) return `${(r/1e3).toFixed(1)} KH/s`;
  return `${r.toFixed(0)} H/s`;
}

// ── Mining loop ───────────────────────────────────────────────────────────────
async function mineLoop() {
  if (!mining) return;
  const c = cfg();
  let tpl;
  try {
    tpl = await rpcCall(c.host, c.port, c.user, c.pass, 'getblocktemplate', [{rules:['segwit']}]);
  } catch(e) {
    log('getblocktemplate error: ' + e.message, true);
    if (mining) miningTimeout = setTimeout(mineLoop, 3000);
    return;
  }

  extraNonce++;
  const spk   = p2pkhScript(c.addr);
  const cbTx  = buildCoinbase(tpl.height, tpl.coinbasevalue, spk, extraNonce);
  const cbTxid = sha256d(cbTx);

  const txids = [cbTxid];
  const txDatas = [cbTx];
  for (const tx of (tpl.transactions || [])) {
    txids.push(Buffer.from(tx.txid, 'hex').reverse());
    txDatas.push(Buffer.from(tx.data, 'hex'));
  }

  const mr     = merkleRoot(txids);
  const prevH  = Buffer.from(tpl.previousblockhash, 'hex').reverse();
  const verBuf = Buffer.alloc(4); verBuf.writeUInt32LE(tpl.version);
  const tBuf   = Buffer.alloc(4); tBuf.writeUInt32LE(tpl.curtime);
  const bitsBuf = Buffer.from(tpl.bits, 'hex').reverse();

  const hdr76 = Buffer.concat([verBuf, prevH, mr, tBuf, bitsBuf]);
  const target = bitsToTargetBigInt(tpl.bits);

  const hdr = Buffer.concat([hdr76, Buffer.alloc(4)]);
  const t0 = Date.now();
  let n = 0;

  // Mine in chunks, yielding to the event loop every 5000 hashes
  const CHUNK = 5000;
  async function mineChunk() {
    if (!mining) return;
    const limit = Math.min(n + CHUNK, 0xffffffff + 1);
    while (n < limit) {
      hdr.writeUInt32LE(n, 76);
      const h = sha256d(hdr);
      const val = BigInt('0x' + h.reverse().toString('hex'));
      if (val < target) {
        // Found!
        const blockHex = Buffer.concat([
          hdr76, Buffer.from(hdr.slice(76, 80)),
          varint(txDatas.length),
          ...txDatas,
        ]).toString('hex');
        try {
          await rpcCall(c.host, c.port, c.user, c.pass, 'submitblock', [blockHex]);
          blocksFound++;
          $('s-blocks').textContent = blocksFound;
          log(`★ BLOCK FOUND! nonce=${n}  hash=${h.toString('hex').slice(0,20)}...`);
        } catch(e) {
          log('submitblock: ' + e.message, true);
        }
        if (mining) miningTimeout = setTimeout(mineLoop, 100);
        return;
      }
      n++;
    }
    const elapsed = (Date.now() - t0) / 1000;
    hashRate = n / elapsed;
    $('s-rate').textContent = fmtRate(hashRate);
    if (n >= 0xffffffff) {
      // Exhausted all nonces — get new template (extra nonce changed)
      if (mining) miningTimeout = setTimeout(mineLoop, 0);
    } else {
      if (mining) miningTimeout = setTimeout(mineChunk, 0);
    }
  }
  mineChunk();
}

// ── Stats poll ────────────────────────────────────────────────────────────────
function startStatsPoll() {
  statInterval = setInterval(async () => {
    if (!mining) return;
    const c = cfg();
    try {
      const [bal, info] = await Promise.all([
        rpcCall(c.host, c.port, c.user, c.pass, 'getbalance'),
        rpcCall(c.host, c.port, c.user, c.pass, 'getblockchaininfo'),
      ]);
      $('s-bal').textContent = parseFloat(bal).toFixed(4);
      $('s-height').textContent = info.blocks;
    } catch(_) {}
    if (startTime) {
      const up = Math.floor((Date.now() - startTime) / 1000);
      const h = String(Math.floor(up/3600)).padStart(2,'0');
      const m = String(Math.floor((up%3600)/60)).padStart(2,'0');
      const s = String(up%60).padStart(2,'0');
    }
  }, 2000);
}

// ── Button handlers ───────────────────────────────────────────────────────────
$('btn-start').addEventListener('click', async () => {
  const c = cfg();
  if (!c.addr) {
    try {
      const wallets = await rpcCall(c.host, c.port, c.user, c.pass, 'listwallets');
      if (!wallets.length)
        await rpcCall(c.host, c.port, c.user, c.pass, 'createwallet', ['miner']);
      const addr = await rpcCall(c.host, c.port, c.user, c.pass, 'getnewaddress');
      $('i-addr').value = addr;
      log(`Using address: ${addr}`);
    } catch(e) { log('Error getting address: ' + e.message, true); return; }
  }
  try {
    const info = await rpcCall(c.host, c.port, c.user, c.pass, 'getblockchaininfo');
    log(`Connected — Gregcoin chain height ${info.blocks}`);
  } catch(e) { log('Cannot connect to node: ' + e.message, true); return; }

  mining = true;
  startTime = Date.now();
  blocksFound = 0;
  $('btn-start').disabled = true;
  $('btn-stop').disabled  = false;
  setStatus('Mining', true);
  startStatsPoll();
  mineLoop();
});

$('btn-stop').addEventListener('click', () => {
  mining = false;
  clearTimeout(miningTimeout);
  clearInterval(statInterval);
  $('btn-start').disabled = false;
  $('btn-stop').disabled  = true;
  $('s-rate').textContent = '0 H/s';
  setStatus('Stopped', false);
  log('Mining stopped');
});
