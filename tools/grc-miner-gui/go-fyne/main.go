// Gregcoin (GRC) GUI — Miner + Wallet — Go + Fyne
//
// Build with embedded daemon (Linux ARM64, run from gregcoin repo root):
//   cp build/bin/gregcoind tools/grc-miner-gui/go-fyne/gregcoind
//   cd tools/grc-miner-gui/go-fyne
//   go build -tags embed_daemon -o gregminer .
//   rm gregcoind   # remove from source tree after build
//
// Build without daemon (development / daemon on PATH):
//   go build -o gregminer .
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
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

// ── App config ────────────────────────────────────────────────────────────────

type AppConfig struct {
	RPCUser        string `json:"rpc_user"`
	RPCPass        string `json:"rpc_pass"`
	RPCHost        string `json:"rpc_host"`
	RPCPort        int    `json:"rpc_port"`
	MiningAddress  string `json:"mining_address"`
	ReceiveAddress string `json:"receive_address"`
}

func appDataDir() string {
	if runtime.GOOS == "windows" {
		return filepath.Join(os.Getenv("APPDATA"), "Gregcoin")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".gregcoin")
}

func configFilePath() string {
	return filepath.Join(appDataDir(), "app_config.json")
}

func randomPass() string {
	b := make([]byte, 18)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func loadOrCreateConfig() AppConfig {
	data, err := os.ReadFile(configFilePath())
	if err == nil {
		var cfg AppConfig
		if json.Unmarshal(data, &cfg) == nil && cfg.RPCPass != "" {
			return cfg
		}
	}
	cfg := AppConfig{
		RPCUser: "grcuser",
		RPCPass: randomPass(),
		RPCHost: "127.0.0.1",
		RPCPort: 8445,
	}
	saveConfig(cfg)
	return cfg
}

func saveConfig(cfg AppConfig) {
	os.MkdirAll(appDataDir(), 0700)
	data, _ := json.MarshalIndent(cfg, "", "  ")
	os.WriteFile(configFilePath(), data, 0600)
}

// ── Daemon management ─────────────────────────────────────────────────────────

var daemonCmd *exec.Cmd

// extractEmbeddedDaemon writes the embedded daemon binary to the app data dir
// (if embeddedDaemon is non-empty) and returns its path. Returns ("", nil) when
// no binary is embedded so the caller can fall through to normal search.
func extractEmbeddedDaemon() (string, error) {
	if len(embeddedDaemon) == 0 {
		return "", nil
	}
	name := "gregcoind"
	if runtime.GOOS == "windows" {
		name = "gregcoind.exe"
	}
	dest := filepath.Join(appDataDir(), name)
	// Skip extraction if the file already exists with the same size.
	if info, err := os.Stat(dest); err == nil && info.Size() == int64(len(embeddedDaemon)) {
		return dest, nil
	}
	if err := os.MkdirAll(appDataDir(), 0700); err != nil {
		return "", err
	}
	if err := os.WriteFile(dest, embeddedDaemon, 0755); err != nil {
		return "", fmt.Errorf("extract embedded daemon: %w", err)
	}
	return dest, nil
}

// findDaemonExe looks for gregcoind/bitcoind in several locations.
func findDaemonExe() (string, error) {
	// 0. Embedded binary (present when built with -tags embed_daemon).
	if path, err := extractEmbeddedDaemon(); err == nil && path != "" {
		return path, nil
	}

	var names []string
	if runtime.GOOS == "windows" {
		names = []string{"gregcoind.exe", "bitcoind.exe"}
	} else {
		names = []string{"gregcoind", "bitcoind"}
	}

	// 1. Next to the running executable (works for deployed builds).
	if exe, err := os.Executable(); err == nil {
		dir := filepath.Dir(exe)
		for _, name := range names {
			p := filepath.Join(dir, name)
			if _, err := os.Stat(p); err == nil {
				return p, nil
			}
		}
	}

	// 2. Walk up from cwd looking for build/bin/ or build/src/ (covers `go run .` from source tree).
	if cwd, err := os.Getwd(); err == nil {
		dir := cwd
		for i := 0; i < 6; i++ {
			for _, name := range names {
				for _, subdir := range []string{"build/bin", "build/src"} {
					p := filepath.Join(dir, subdir, name)
					if _, err := os.Stat(p); err == nil {
						return p, nil
					}
				}
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}

	// 3. PATH
	for _, name := range names {
		if p, err := exec.LookPath(name); err == nil {
			return p, nil
		}
	}

	if runtime.GOOS == "windows" {
		return "", fmt.Errorf("gregcoind not found — place gregcoind.exe in the same folder as this program")
	}
	return "", fmt.Errorf("gregcoind not found — build it (cmake --build build) or put it in PATH")
}

func writeDaemonConf(cfg AppConfig) error {
	dd := appDataDir()
	if err := os.MkdirAll(dd, 0700); err != nil {
		return err
	}
	conf := fmt.Sprintf(
		// bind=0.0.0.0 prevents the daemon auto-binding 127.0.0.1:<rpcport> for Tor
		// (default onion port = P2P port+1 = 8445, which collides with rpcport=8445).
		"rpcuser=%s\nrpcpassword=%s\nrpcport=%d\nrpcbind=127.0.0.1\nrpcallowip=127.0.0.1\nserver=1\nlisten=1\nbind=0.0.0.0\nlistenonion=0\nmaxconnections=16\n",
		cfg.RPCUser, cfg.RPCPass, cfg.RPCPort,
	)
	// Daemon reads gregcoin.conf (renamed from bitcoin.conf in this fork).
	// Do NOT write bitcoin.conf — the daemon reads both and duplicate rpcbind breaks startup.
	return os.WriteFile(filepath.Join(dd, "gregcoin.conf"), []byte(conf), 0600)
}

// startDaemon starts gregcoind; returns the exe path found or an error.
func startDaemon(cfg AppConfig) (string, error) {
	exePath, err := findDaemonExe()
	if err != nil {
		return "", err
	}
	if err := writeDaemonConf(cfg); err != nil {
		return exePath, fmt.Errorf("write config: %w", err)
	}
	daemonCmd = exec.Command(exePath, "-datadir="+appDataDir())
	if err := daemonCmd.Start(); err != nil {
		daemonCmd = nil
		return exePath, fmt.Errorf("start daemon: %w", err)
	}
	return exePath, nil
}

func stopDaemon() {
	if daemonCmd != nil && daemonCmd.Process != nil {
		daemonCmd.Process.Kill()
		daemonCmd.Wait()
		daemonCmd = nil
	}
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
		"jsonrpc": "1.0", "id": r.id,
		"method": method, "params": params,
	})
	req, _ := http.NewRequest("POST", r.url, bytes.NewReader(body))
	req.SetBasicAuth(r.user, r.pass)
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cannot reach daemon at %s — is it running? (%w)", r.url, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == 401 {
		return nil, fmt.Errorf("RPC authentication failed — check username/password")
	}

	data, _ := io.ReadAll(resp.Body)
	if len(data) == 0 {
		return nil, fmt.Errorf("empty response from daemon (HTTP %d)", resp.StatusCode)
	}

	var result struct {
		Result json.RawMessage `json:"result"`
		Error  interface{}     `json:"error"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, fmt.Errorf("invalid response from daemon (HTTP %d): %w", resp.StatusCode, err)
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
	// Load or create persistent config (RPC credentials stored in AppData/Gregcoin)
	cfg := loadOrCreateConfig()

	a := app.New()
	a.Settings().SetTheme(theme.DarkTheme())
	w := a.NewWindow("Gregcoin GRC")
	w.Resize(fyne.NewSize(540, 760))

	// ── Connection fields ───────────────────────────────────────────────────

	hostEntry := widget.NewEntry()
	hostEntry.SetText(cfg.RPCHost)
	portEntry := widget.NewEntry()
	portEntry.SetText(fmt.Sprintf("%d", cfg.RPCPort))
	userEntry := widget.NewEntry()
	userEntry.SetText(cfg.RPCUser)
	passEntry := widget.NewPasswordEntry()
	passEntry.SetText(cfg.RPCPass)

	nodeStatusLabel := widget.NewLabelWithStyle("● Starting…", fyne.TextAlignLeading, fyne.TextStyle{Monospace: true})

	connForm := widget.NewForm(
		widget.NewFormItem("Host",     hostEntry),
		widget.NewFormItem("RPC Port", portEntry),
		widget.NewFormItem("User",     userEntry),
		widget.NewFormItem("Password", passEntry),
		widget.NewFormItem("Daemon",   nodeStatusLabel),
	)

	var rpc       *rpcClient // node-level RPC (no wallet path)
	var walletRPC *rpcClient // wallet-level RPC (URL includes /wallet/<name>/)

	getOrCreateRPC := func() *rpcClient {
		var port int
		fmt.Sscan(portEntry.Text, &port)
		// Persist any user-edited connection settings
		cfg.RPCHost = hostEntry.Text
		cfg.RPCPort = port
		cfg.RPCUser = userEntry.Text
		cfg.RPCPass = passEntry.Text
		saveConfig(cfg)
		return newRPC(cfg.RPCHost, cfg.RPCPort, cfg.RPCUser, cfg.RPCPass)
	}

	ensureWallet := func(r *rpcClient) {
		wallets, _ := r.Call("listwallets")
		var wlist []string
		json.Unmarshal(wallets, &wlist)
		var walletName string
		if len(wlist) == 0 {
			// Try to load an existing wallet file first; create only if it doesn't exist.
			if _, err := r.Call("loadwallet", "main"); err != nil {
				r.Call("createwallet", "main")
			}
			walletName = "main"
		} else {
			// Use the first loaded wallet (prefer "main" if present).
			walletName = wlist[0]
			for _, w := range wlist {
				if w == "main" {
					walletName = "main"
					break
				}
			}
		}
		// Build a wallet-specific RPC client so calls work even when
		// multiple wallets are loaded simultaneously.
		walletRPC = &rpcClient{
			url:  fmt.Sprintf("http://%s:%d/wallet/%s/", cfg.RPCHost, cfg.RPCPort, walletName),
			user: cfg.RPCUser,
			pass: cfg.RPCPass,
		}
	}

	// ── Auto-start daemon ───────────────────────────────────────────────────

	// Declared here so the daemon goroutine below can reference it; assigned later.
	var refreshAddresses func()

	// Closed once the daemon is confirmed ready; lets startBtn wait safely.
	daemonReady := make(chan struct{})

	go func() {
		exePath, err := startDaemon(cfg)
		if err != nil {
			nodeStatusLabel.SetText("● " + err.Error())
			return
		}
		nodeStatusLabel.SetText("● Starting " + filepath.Base(exePath) + "…")
		// Poll up to 15s for daemon to become ready
		testRPC := newRPC(cfg.RPCHost, cfg.RPCPort, cfg.RPCUser, cfg.RPCPass)
		for i := 0; i < 30; i++ {
			time.Sleep(500 * time.Millisecond)
			if _, err := testRPC.Call("getblockchaininfo"); err == nil {
				// Ensure wallet is loaded so the wallet tab works immediately.
				ensureWallet(testRPC)
				rpc = testRPC
				nodeStatusLabel.SetText("● Running (" + filepath.Base(exePath) + ")")
				close(daemonReady)
				go refreshAddresses()
				return
			}
		}
		nodeStatusLabel.SetText("● Running (check connection if errors occur)")
	}()

	w.SetOnClosed(func() {
		stopDaemon()
	})

	// ── Mine tab ───────────────────────────────────────────────────────────

	addrEntry := widget.NewEntry()
	addrEntry.SetPlaceHolder("Auto-generated on first mine")
	if cfg.MiningAddress != "" {
		addrEntry.SetText(cfg.MiningAddress)
	}
	addrEntry.OnChanged = func(s string) {
		cfg.MiningAddress = strings.TrimSpace(s)
		saveConfig(cfg)
	}

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
		// Wait for daemon if not yet ready (up to 15s).
		select {
		case <-daemonReady:
			// Already ready — fast path.
		default:
			addLog("Waiting for daemon…")
			select {
			case <-daemonReady:
				// Became ready in time.
			case <-time.After(15 * time.Second):
				addLog("ERROR: Daemon did not start in time — check the Daemon status above")
				return
			}
		}
		if rpc == nil {
			rpc = getOrCreateRPC()
			ensureWallet(rpc)
		}

		addr := strings.TrimSpace(addrEntry.Text)
		if addr == "" {
			if walletRPC == nil {
				addLog("ERROR: wallet not ready")
				return
			}
			raw, err := walletRPC.Call("getnewaddress")
			if err != nil {
				addLog("ERROR getting address: " + err.Error())
				return
			}
			json.Unmarshal(raw, &addr)
			addrEntry.SetText(addr) // OnChanged saves to cfg automatically
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

	balLabel      := widget.NewLabelWithStyle("— GRC", fyne.TextAlignLeading, fyne.TextStyle{Bold: true, Monospace: true})
	immatureLabel := widget.NewLabelWithStyle("", fyne.TextAlignLeading, fyne.TextStyle{Monospace: true})
	receiveAddr   := widget.NewEntry()
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
		wr := walletRPC
		if wr == nil {
			return
		}
		if raw, err := wr.Call("getbalances"); err == nil {
			var bals struct {
				Mine struct {
					Trusted   float64 `json:"trusted"`
					Immature  float64 `json:"immature"`
				} `json:"mine"`
			}
			json.Unmarshal(raw, &bals)
			balLabel.SetText(fmt.Sprintf("%.8f GRC", bals.Mine.Trusted))
			if bals.Mine.Immature > 0 {
				immatureLabel.SetText(fmt.Sprintf("+ %.8f GRC immature (maturing)", bals.Mine.Immature))
			} else {
				immatureLabel.SetText("")
			}
		}
		// Only fetch a new receive address on true first-ever run.
		if cfg.ReceiveAddress == "" {
			if raw, err := wr.Call("getnewaddress"); err == nil {
				json.Unmarshal(raw, &cfg.ReceiveAddress)
				saveConfig(cfg)
			}
		}
		receiveAddr.Enable()
		receiveAddr.SetText(cfg.ReceiveAddress)
		receiveAddr.Disable()
		if raw, err := wr.Call("listtransactions", "*", 20, 0); err == nil {
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
			ensureWallet(rpc)
		}
		wr := walletRPC
		if wr == nil {
			dialog.ShowError(fmt.Errorf("wallet not ready — try refreshing first"), w)
			return
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
		raw, err := wr.Call("sendtoaddress", to, amount)
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
		widget.NewCard("Balance", "", container.NewVBox(
			container.NewHBox(balLabel, widget.NewLabel(""), refreshBtn),
			immatureLabel,
		)),
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

	// ── Addresses tab ──────────────────────────────────────────────────────

	addrList := widget.NewMultiLineEntry()
	addrList.Disable()
	addrListScroll := container.NewVScroll(addrList)
	addrListScroll.SetMinSize(fyne.NewSize(500, 400))

	addrStatusLabel := widget.NewLabel("Press Refresh to load addresses.")

	refreshAddresses = func() {
		if rpc == nil {
			rpc = getOrCreateRPC()
			ensureWallet(rpc)
		}
		wr := walletRPC
		if wr == nil {
			addrStatusLabel.SetText("Wallet not ready.")
			return
		}
		addrStatusLabel.SetText("Loading…")
		raw, err := wr.Call("listreceivedbyaddress", 0, true)
		if err != nil {
			addrStatusLabel.SetText("Error: " + err.Error())
			return
		}
		var entries []struct {
			Address       string  `json:"address"`
			Amount        float64 `json:"amount"`
			Confirmations int     `json:"confirmations"`
			TxIDs         []string `json:"txids"`
			Label         string  `json:"label"`
		}
		json.Unmarshal(raw, &entries)

		var sb strings.Builder
		sb.WriteString(fmt.Sprintf("%-46s  %18s  %s\n", "Address", "Received (GRC)", "TXs"))
		sb.WriteString(strings.Repeat("─", 80) + "\n")
		used := 0
		for _, e := range entries {
			marker := " "
			if len(e.TxIDs) > 0 {
				marker = "●"
				used++
			}
			sb.WriteString(fmt.Sprintf("%s %-46s  %18.8f  %d\n",
				marker, e.Address, e.Amount, len(e.TxIDs)))
		}
		sb.WriteString(fmt.Sprintf("\n%d addresses total, %d used (● = has transactions)\n", len(entries), used))

		addrList.Enable()
		addrList.SetText(sb.String())
		addrList.Disable()
		addrStatusLabel.SetText(fmt.Sprintf("%d addresses", len(entries)))
	}

	addrRefreshBtn := widget.NewButton("⟳  Refresh Addresses", func() { go refreshAddresses() })

	newAddrBtn := widget.NewButton("+ New Address", func() {
		if rpc == nil {
			rpc = getOrCreateRPC()
			ensureWallet(rpc)
		}
		wr := walletRPC
		if wr == nil {
			addrStatusLabel.SetText("Wallet not ready.")
			return
		}
		raw, err := wr.Call("getnewaddress")
		if err != nil {
			addrStatusLabel.SetText("Error: " + err.Error())
			return
		}
		var addr string
		json.Unmarshal(raw, &addr)
		// Persist the new receive address so the wallet tab also shows it.
		cfg.ReceiveAddress = addr
		saveConfig(cfg)
		go refreshAddresses()
	})

	addressesTab := container.NewVBox(
		container.NewHBox(addrRefreshBtn, newAddrBtn, addrStatusLabel),
		widget.NewCard("All Wallet Addresses", "● = has received transactions", addrListScroll),
	)

	// ── Assemble ───────────────────────────────────────────────────────────

	tabs := container.NewAppTabs(
		container.NewTabItem("⛏  Mine",      container.NewVScroll(mineTab)),
		container.NewTabItem("💰 Wallet",    container.NewVScroll(walletTab)),
		container.NewTabItem("📋 Addresses", container.NewVScroll(addressesTab)),
	)
	tabs.SetTabLocation(container.TabLocationTop)
	tabs.OnChanged = func(tab *container.TabItem) {
		if tab.Text == "📋 Addresses" {
			go refreshAddresses()
		}
	}

	w.SetContent(container.NewBorder(
		widget.NewCard("Node Connection", "", connForm),
		nil, nil, nil,
		tabs,
	))

	w.ShowAndRun()
}
