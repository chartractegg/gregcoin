// Gregcoin (GRC) GUI — Miner + Wallet — Go + Fyne
//
// Build (Windows, from Linux with mingw):
//   GOOS=windows GOARCH=amd64 CGO_ENABLED=1 CC=x86_64-w64-mingw32-gcc go build -ldflags="-H windowsgui" -o Gregminer.exe .
//
// Build (native):
//   go build -o gregminer .
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// ── SHA256d ───────────────────────────────────────────────────────────────────

func sha256d(data []byte) []byte {
	h1 := sha256.Sum256(data)
	h2 := sha256.Sum256(h1[:])
	return h2[:]
}

// ── Varint ────────────────────────────────────────────────────────────────────

func varint(n uint64) []byte {
	switch {
	case n < 0xfd:
		return []byte{byte(n)}
	case n <= 0xffff:
		b := make([]byte, 3)
		b[0] = 0xfd
		binary.LittleEndian.PutUint16(b[1:], uint16(n))
		return b
	default:
		b := make([]byte, 5)
		b[0] = 0xfe
		binary.LittleEndian.PutUint32(b[1:], uint32(n))
		return b
	}
}

// ── Merkle root ───────────────────────────────────────────────────────────────

func merkleRoot(txids [][]byte) []byte {
	if len(txids) == 0 {
		return make([]byte, 32)
	}
	layer := make([][]byte, len(txids))
	copy(layer, txids)
	for len(layer) > 1 {
		if len(layer)%2 == 1 {
			layer = append(layer, layer[len(layer)-1])
		}
		next := make([][]byte, len(layer)/2)
		for i := range next {
			next[i] = sha256d(append(layer[i*2], layer[i*2+1]...))
		}
		layer = next
	}
	return layer[0]
}

// ── nBits → target ────────────────────────────────────────────────────────────

func bitsToTarget(bitsHex string) *big.Int {
	b, _ := hex.DecodeString(bitsHex)
	if len(b) < 4 {
		return new(big.Int)
	}
	exp  := int(b[0])
	mant := new(big.Int).SetBytes(b[1:4])
	shift := new(big.Int).Exp(big.NewInt(256), big.NewInt(int64(exp-3)), nil)
	return new(big.Int).Mul(mant, shift)
}

// ── Coinbase transaction ──────────────────────────────────────────────────────

func buildCoinbase(height, coinbaseValue int64, scriptPubKey []byte, extraNonce uint32) []byte {
	var hBytes []byte
	v := height
	for v > 0 {
		hBytes = append(hBytes, byte(v&0xff))
		v >>= 8
	}
	enBytes := make([]byte, 4)
	binary.LittleEndian.PutUint32(enBytes, extraNonce)

	scriptSig := append([]byte{byte(len(hBytes))}, hBytes...)
	scriptSig  = append(scriptSig, 4)
	scriptSig  = append(scriptSig, enBytes...)

	var tx []byte
	ver := make([]byte, 4)
	binary.LittleEndian.PutUint32(ver, 1)
	tx = append(tx, ver...)
	tx = append(tx, varint(1)...)
	tx = append(tx, make([]byte, 32)...)
	tx = append(tx, 0xff, 0xff, 0xff, 0xff)
	tx = append(tx, varint(uint64(len(scriptSig)))...)
	tx = append(tx, scriptSig...)
	tx = append(tx, 0xff, 0xff, 0xff, 0xff)
	tx = append(tx, varint(1)...)
	val := make([]byte, 8)
	binary.LittleEndian.PutUint64(val, uint64(coinbaseValue))
	tx = append(tx, val...)
	tx = append(tx, varint(uint64(len(scriptPubKey)))...)
	tx = append(tx, scriptPubKey...)
	tx = append(tx, make([]byte, 4)...)
	return tx
}

// ── Base58 → P2PKH scriptPubKey ───────────────────────────────────────────────

const b58Chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

func p2pkhScript(address string) []byte {
	n := new(big.Int)
	for _, c := range address {
		n.Mul(n, big.NewInt(58))
		n.Add(n, big.NewInt(int64(strings.IndexRune(b58Chars, c))))
	}
	raw := n.Bytes()
	if len(raw) < 25 {
		pad := make([]byte, 25-len(raw))
		raw = append(pad, raw...)
	}
	h160 := raw[1:21]
	script := []byte{0x76, 0xa9, 0x14}
	script  = append(script, h160...)
	script  = append(script, 0x88, 0xac)
	return script
}

func rev(b []byte) []byte {
	r := make([]byte, len(b))
	for i, v := range b {
		r[len(b)-1-i] = v
	}
	return r
}

// ── RPC ───────────────────────────────────────────────────────────────────────

type rpcClient struct {
	url  string
	user string
	pass string
	id   int
}

func newRPC(host string, port int, user, pass string) *rpcClient {
	return &rpcClient{
		url:  fmt.Sprintf("http://%s:%d/", host, port),
		user: user,
		pass: pass,
	}
}

func (r *rpcClient) Call(method string, params ...interface{}) (json.RawMessage, error) {
	r.id++
	body, _ := json.Marshal(map[string]interface{}{
		"jsonrpc": "1.1", "id": r.id,
		"method": method, "params": params,
	})
	req, _ := http.NewRequest("POST", r.url, bytes.NewReader(body))
	req.SetBasicAuth(r.user, r.pass)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var result struct {
		Result json.RawMessage `json:"result"`
		Error  interface{}     `json:"error"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, err
	}
	if result.Error != nil {
		return nil, fmt.Errorf("rpc: %v", result.Error)
	}
	return result.Result, nil
}

// ── Miner ─────────────────────────────────────────────────────────────────────

type Miner struct {
	rpc         *rpcClient
	address     string
	running     atomic.Bool
	hashCount   atomic.Int64
	blocksFound atomic.Int64
	onBlock     func(hash string)
	onError     func(err string)
}

func (m *Miner) Start() {
	if m.running.Swap(true) {
		return
	}
	go m.loop()
}

func (m *Miner) Stop() { m.running.Store(false) }

func (m *Miner) loop() {
	var extraNonce uint32
	for m.running.Load() {
		extraNonce++
		tplRaw, err := m.rpc.Call("getblocktemplate", map[string]interface{}{"rules": []string{"segwit"}})
		if err != nil {
			m.onError(err.Error())
			time.Sleep(3 * time.Second)
			continue
		}
		var tpl struct {
			Version           int    `json:"version"`
			PreviousBlockHash string `json:"previousblockhash"`
			Transactions      []struct {
				Data string `json:"data"`
				TxID string `json:"txid"`
			} `json:"transactions"`
			CoinbaseValue int64  `json:"coinbasevalue"`
			Bits          string `json:"bits"`
			Height        int64  `json:"height"`
			CurTime       uint32 `json:"curtime"`
		}
		if err := json.Unmarshal(tplRaw, &tpl); err != nil {
			m.onError("parse template: " + err.Error())
			time.Sleep(2 * time.Second)
			continue
		}

		spk   := p2pkhScript(m.address)
		cbTx  := buildCoinbase(tpl.Height, tpl.CoinbaseValue, spk, extraNonce)
		cbTxid := sha256d(cbTx)

		txids   := [][]byte{cbTxid}
		txDatas := [][]byte{cbTx}
		for _, tx := range tpl.Transactions {
			raw, _ := hex.DecodeString(tx.Data)
			txDatas  = append(txDatas, raw)
			txid, _  := hex.DecodeString(tx.TxID)
			txids     = append(txids, rev(txid))
		}

		mr      := merkleRoot(txids)
		prevH, _ := hex.DecodeString(tpl.PreviousBlockHash)
		prevH    = rev(prevH)
		bitsB, _ := hex.DecodeString(tpl.Bits)
		bitsLE   := rev(bitsB)

		hdr76 := make([]byte, 76)
		binary.LittleEndian.PutUint32(hdr76[0:], uint32(tpl.Version))
		copy(hdr76[4:36], prevH)
		copy(hdr76[36:68], mr)
		binary.LittleEndian.PutUint32(hdr76[68:], tpl.CurTime)
		copy(hdr76[72:76], bitsLE)

		target := bitsToTarget(tpl.Bits)
		hdr    := make([]byte, 80)
		copy(hdr, hdr76)

		for nonce := uint32(0); m.running.Load(); nonce++ {
			binary.LittleEndian.PutUint32(hdr[76:], nonce)
			h := sha256d(hdr)
			m.hashCount.Add(1)

			if new(big.Int).SetBytes(rev(h)).Cmp(target) < 0 {
				noncePart := make([]byte, 4)
				binary.LittleEndian.PutUint32(noncePart, nonce)
				var blockBuf []byte
				blockBuf = append(blockBuf, hdr76...)
				blockBuf = append(blockBuf, noncePart...)
				blockBuf = append(blockBuf, varint(uint64(len(txDatas)))...)
				for _, tx := range txDatas {
					blockBuf = append(blockBuf, tx...)
				}
				_, err := m.rpc.Call("submitblock", hex.EncodeToString(blockBuf))
				if err == nil || errors.Is(err, nil) {
					m.blocksFound.Add(1)
					m.onBlock(hex.EncodeToString(rev(sha256d(hdr))))
				} else {
					m.onError("submitblock: " + err.Error())
				}
				break
			}
			if nonce == 0xffffffff {
				break
			}
		}
	}
}

// ── GUI ───────────────────────────────────────────────────────────────────────

func main() {
	a := app.New()
	a.Settings().SetTheme(theme.DarkTheme())
	w := a.NewWindow("Gregcoin GRC")
	w.Resize(fyne.NewSize(540, 720))

	// ── Connection fields (shared) ──────────────────────────────────────────

	hostEntry := widget.NewEntry()
	hostEntry.SetText("127.0.0.1")
	portEntry := widget.NewEntry()
	portEntry.SetText("8445")
	userEntry := widget.NewEntry()
	userEntry.SetText("grcuser")
	passEntry := widget.NewPasswordEntry()

	connForm := widget.NewForm(
		widget.NewFormItem("Host",     hostEntry),
		widget.NewFormItem("RPC Port", portEntry),
		widget.NewFormItem("User",     userEntry),
		widget.NewFormItem("Password", passEntry),
	)

	var rpc *rpcClient

	getOrCreateRPC := func() *rpcClient {
		var port int
		fmt.Sscan(portEntry.Text, &port)
		return newRPC(hostEntry.Text, port, userEntry.Text, passEntry.Text)
	}

	ensureWallet := func(r *rpcClient) {
		wallets, _ := r.Call("listwallets")
		var wlist []string
		json.Unmarshal(wallets, &wlist)
		if len(wlist) == 0 {
			r.Call("createwallet", "main")
		}
	}

	// ── Mine tab ───────────────────────────────────────────────────────────

	addrEntry  := widget.NewEntry()
	addrEntry.SetPlaceHolder("Leave blank to auto-generate")

	rateLabel   := widget.NewLabelWithStyle("0 H/s", fyne.TextAlignLeading, fyne.TextStyle{Monospace: true})
	blocksLabel := widget.NewLabelWithStyle("0",     fyne.TextAlignLeading, fyne.TextStyle{Monospace: true})
	heightLabel := widget.NewLabelWithStyle("—",     fyne.TextAlignLeading, fyne.TextStyle{Monospace: true})
	statusLabel := widget.NewLabelWithStyle("Stopped", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})

	statsGrid := widget.NewForm(
		widget.NewFormItem("Hash Rate", rateLabel),
		widget.NewFormItem("Blocks Found", blocksLabel),
		widget.NewFormItem("Chain Height", heightLabel),
		widget.NewFormItem("Status", statusLabel),
	)

	logEntry := widget.NewMultiLineEntry()
	logEntry.Disable()
	logScroll := container.NewVScroll(logEntry)
	logScroll.SetMinSize(fyne.NewSize(500, 140))

	addLog := func(msg string) {
		ts := time.Now().Format("15:04:05")
		logEntry.Enable()
		logEntry.SetText(logEntry.Text + fmt.Sprintf("[%s] %s\n", ts, msg))
		logEntry.CursorRow = len(strings.Split(logEntry.Text, "\n"))
		logEntry.Disable()
	}

	var miner *Miner
	startBtn := widget.NewButton("▶  Start Mining", nil)
	stopBtn  := widget.NewButton("◼  Stop", nil)
	stopBtn.Disable()

	startBtn.OnTapped = func() {
		rpc = getOrCreateRPC()
		ensureWallet(rpc)

		addr := strings.TrimSpace(addrEntry.Text)
		if addr == "" {
			raw, err := rpc.Call("getnewaddress")
			if err != nil {
				addLog("ERROR: " + err.Error())
				return
			}
			json.Unmarshal(raw, &addr)
			addrEntry.SetText(addr)
			addLog("Generated address: " + addr)
		}

		infoRaw, err := rpc.Call("getblockchaininfo")
		if err != nil {
			addLog("Cannot connect: " + err.Error())
			return
		}
		var info struct{ Blocks int `json:"blocks"` }
		json.Unmarshal(infoRaw, &info)
		addLog(fmt.Sprintf("Connected — height %d", info.Blocks))

		miner = &Miner{
			rpc:     rpc,
			address: addr,
			onBlock: func(hash string) {
				addLog(fmt.Sprintf("★ BLOCK FOUND! %s…", hash[:16]))
				blocksLabel.SetText(fmt.Sprintf("%d", miner.blocksFound.Load()))
			},
			onError: func(e string) { addLog("ERR: " + e) },
		}
		miner.Start()
		startBtn.Disable()
		stopBtn.Enable()
		statusLabel.SetText("⛏  Mining…")
		addLog("Mining started")
	}

	stopBtn.OnTapped = func() {
		if miner != nil {
			miner.Stop()
			miner = nil
		}
		startBtn.Enable()
		stopBtn.Disable()
		statusLabel.SetText("Stopped")
		rateLabel.SetText("0 H/s")
		addLog("Mining stopped")
	}

	// Hash rate + height poller
	go func() {
		var lastCount int64
		for {
			time.Sleep(time.Second)
			if miner == nil || rpc == nil {
				continue
			}
			cur  := miner.hashCount.Load()
			rate := float64(cur - lastCount)
			lastCount = cur
			switch {
			case rate >= 1e6:
				rateLabel.SetText(fmt.Sprintf("%.2f MH/s", rate/1e6))
			case rate >= 1e3:
				rateLabel.SetText(fmt.Sprintf("%.1f KH/s", rate/1e3))
			default:
				rateLabel.SetText(fmt.Sprintf("%.0f H/s", rate))
			}
			if raw, err := rpc.Call("getblockchaininfo"); err == nil {
				var info struct{ Blocks int `json:"blocks"` }
				json.Unmarshal(raw, &info)
				heightLabel.SetText(fmt.Sprintf("%d", info.Blocks))
			}
		}
	}()

	mineTab := container.NewVBox(
		widget.NewCard("Mining Address", "", widget.NewForm(
			widget.NewFormItem("Address", addrEntry),
		)),
		widget.NewCard("Stats", "", statsGrid),
		widget.NewCard("Log", "", logScroll),
		container.NewGridWithColumns(2, startBtn, stopBtn),
	)

	// ── Wallet tab ─────────────────────────────────────────────────────────

	balLabel   := widget.NewLabelWithStyle("— GRC", fyne.TextAlignLeading, fyne.TextStyle{Bold: true, Monospace: true})
	receiveAddr := widget.NewEntry()
	receiveAddr.Disable()

	sendToEntry  := widget.NewEntry()
	sendToEntry.SetPlaceHolder("G…  recipient address")
	sendAmtEntry := widget.NewEntry()
	sendAmtEntry.SetPlaceHolder("0.0000")

	txList := widget.NewMultiLineEntry()
	txList.Disable()
	txScroll := container.NewVScroll(txList)
	txScroll.SetMinSize(fyne.NewSize(500, 160))

	refreshWallet := func() {
		if rpc == nil {
			rpc = getOrCreateRPC()
			ensureWallet(rpc)
		}
		if raw, err := rpc.Call("getbalance"); err == nil {
			var bal float64
			json.Unmarshal(raw, &bal)
			balLabel.SetText(fmt.Sprintf("%.8f GRC", bal))
		}
		if raw, err := rpc.Call("getnewaddress"); err == nil {
			var addr string
			json.Unmarshal(raw, &addr)
			receiveAddr.Enable()
			receiveAddr.SetText(addr)
			receiveAddr.Disable()
		}
		if raw, err := rpc.Call("listtransactions", "*", 20, 0); err == nil {
			var txs []struct {
				Category string  `json:"category"`
				Amount   float64 `json:"amount"`
				TxID     string  `json:"txid"`
				Time     int64   `json:"time"`
			}
			json.Unmarshal(raw, &txs)
			var sb strings.Builder
			for i := len(txs) - 1; i >= 0; i-- {
				tx := txs[i]
				t  := time.Unix(tx.Time, 0).Format("2006-01-02 15:04")
				sb.WriteString(fmt.Sprintf("[%s] %+.8f GRC  %s  %.8s…\n",
					t, tx.Amount, tx.Category, tx.TxID))
			}
			txList.Enable()
			txList.SetText(sb.String())
			txList.Disable()
		}
	}

	refreshBtn := widget.NewButton("⟳  Refresh", func() { go refreshWallet() })

	copyBtn := widget.NewButton("Copy", func() {
		w.Clipboard().SetContent(receiveAddr.Text)
	})

	sendBtn := widget.NewButton("Send GRC", func() {
		if rpc == nil {
			rpc = getOrCreateRPC()
		}
		to  := strings.TrimSpace(sendToEntry.Text)
		amt := strings.TrimSpace(sendAmtEntry.Text)
		if to == "" || amt == "" {
			dialog.ShowError(fmt.Errorf("enter recipient address and amount"), w)
			return
		}
		var amount float64
		if _, err := fmt.Sscanf(amt, "%f", &amount); err != nil || amount <= 0 {
			dialog.ShowError(fmt.Errorf("invalid amount"), w)
			return
		}
		raw, err := rpc.Call("sendtoaddress", to, amount)
		if err != nil {
			dialog.ShowError(err, w)
			return
		}
		var txid string
		json.Unmarshal(raw, &txid)
		dialog.ShowInformation("Sent", fmt.Sprintf("TX: %s", txid), w)
		sendToEntry.SetText("")
		sendAmtEntry.SetText("")
		go refreshWallet()
	})

	walletTab := container.NewVBox(
		widget.NewCard("Balance", "", container.NewHBox(balLabel, widget.NewLabel(""), refreshBtn)),
		widget.NewCard("Receive", "", container.NewVBox(
			receiveAddr,
			copyBtn,
		)),
		widget.NewCard("Send", "", widget.NewForm(
			widget.NewFormItem("To", sendToEntry),
			widget.NewFormItem("Amount", sendAmtEntry),
			widget.NewFormItem("", sendBtn),
		)),
		widget.NewCard("Recent Transactions", "", txScroll),
	)

	// ── Assemble ───────────────────────────────────────────────────────────

	tabs := container.NewAppTabs(
		container.NewTabItem("⛏  Mine",   container.NewVScroll(mineTab)),
		container.NewTabItem("💰 Wallet", container.NewVScroll(walletTab)),
	)
	tabs.SetTabLocation(container.TabLocationTop)

	w.SetContent(container.NewBorder(
		widget.NewCard("Node Connection", "", connForm),
		nil, nil, nil,
		tabs,
	))

	w.ShowAndRun()
}
