//go:build embed_daemon

package main

import _ "embed"

//go:embed gregcoind
var embeddedDaemon []byte
