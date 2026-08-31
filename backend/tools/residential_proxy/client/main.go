// JobFinder residential proxy — single self-contained client.
//
// Modes:
//   (no args)     first run: copy self into place, register autostart (logon), start hidden, notify
//   --run         the background supervisor: hold a reverse SSH tunnel + serve an HTTP proxy over it
//   --uninstall   stop + remove autostart (turn it off cleanly)
//
// The server egresses to the internet through THIS machine's home connection (residential IP) so it
// can reach ATSes that block the datacenter. The server side binds a loopback SLOT in 8120..8129
// only (one per connected machine) — never public. Reconnects with exponential backoff, forever.
//
// The private key is embedded from key.pem at build time (gitignored). The server host key is pinned
// below (public) so the client can't be MITM'd.
package main

import (
	"bufio"
	"bytes"
	_ "embed"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"

	"golang.org/x/crypto/ssh"
)

//go:embed key.pem
var privKey []byte

const (
	serverAddr    = "proxy.systeam.kz:22"
	tunnelUser    = "tunnel"
	slotBase      = 8120 // loopback slot range 8120..8120+slotCount-1 — one per connected machine
	slotCount     = 10
	taskName      = "JobFinderResidentialProxy"
	serverHostKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJlMT9ZRT/tAUXCbDHAE1Fp9cmCCpndXsICk4EkdU/jV"
)

func main() {
	mode := ""
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	switch mode {
	case "--run":
		supervise()
	case "--uninstall":
		doUninstall()
	default:
		doInstall()
	}
}

// ---- install / autostart -----------------------------------------------------------------------
func installDir() string {
	switch runtime.GOOS {
	case "windows":
		return filepath.Join(os.Getenv("LOCALAPPDATA"), "jobfinder-proxy")
	case "darwin":
		return filepath.Join(os.Getenv("HOME"), "Library", "Application Support", "jobfinder-proxy")
	default:
		return filepath.Join(os.Getenv("HOME"), ".jobfinder-proxy")
	}
}

func agentPath() string {
	name := "agent"
	if runtime.GOOS == "windows" {
		name = "agent.exe"
	}
	return filepath.Join(installDir(), name)
}

func plistPath() string {
	return filepath.Join(os.Getenv("HOME"), "Library", "LaunchAgents", "com.jobfinder.residentialproxy.plist")
}

func writeStatus(s string) {
	os.MkdirAll(installDir(), 0755)
	os.WriteFile(filepath.Join(installDir(), "status.txt"),
		[]byte(time.Now().Format("2006-01-02 15:04:05")+"  "+s+"\n"), 0644)
}

func doInstall() {
	dir := installDir()
	os.MkdirAll(dir, 0755)
	self, _ := os.Executable()
	ap := agentPath()
	if p, _ := filepath.Abs(self); p != ap { // copy self into the install dir
		if data, err := os.ReadFile(self); err == nil {
			os.WriteFile(ap, data, 0755)
		}
	}
	switch runtime.GOOS {
	case "windows":
		tr := "\"" + ap + "\" --run"
		exec.Command("schtasks", "/Create", "/TN", taskName, "/TR", tr,
			"/SC", "ONLOGON", "/RL", "LIMITED", "/F").Run()
		exec.Command("schtasks", "/Run", "/TN", taskName).Run() // start hidden now
	case "darwin":
		writePlist(ap)
		exec.Command("launchctl", "unload", plistPath()).Run()
		exec.Command("launchctl", "load", plistPath()).Run()
	default:
		c := exec.Command(ap, "--run") // linux: no login manager assumed — just start it
		c.Start()
	}
	writeStatus("установлено, подключаюсь…")
	notify("JobFinder residential proxy", "Установлено ✓\nРаботает в фоне, автозапуск при входе включён.\n"+
		"Сервер теперь может ходить через твой домашний IP.\nОтключить — кнопкой «Отключить» на странице загрузки.")
}

func writePlist(ap string) {
	os.MkdirAll(filepath.Dir(plistPath()), 0755)
	pl := `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jobfinder.residentialproxy</string>
  <key>ProgramArguments</key><array><string>` + ap + `</string><string>--run</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>`
	os.WriteFile(plistPath(), []byte(pl), 0644)
}

func doUninstall() {
	switch runtime.GOOS {
	case "windows":
		exec.Command("schtasks", "/End", "/TN", taskName).Run()
		exec.Command("schtasks", "/Delete", "/TN", taskName, "/F").Run()
	case "darwin":
		exec.Command("launchctl", "unload", plistPath()).Run()
		os.Remove(plistPath())
	default:
		exec.Command("pkill", "-f", agentPath()).Run()
	}
	writeStatus("отключено")
	notify("JobFinder residential proxy", "Отключено ✓\nРезидентный прокси остановлен и убран из автозапуска.")
}

// ---- supervisor (the actual tunnel) ------------------------------------------------------------
func supervise() {
	delay := 2 * time.Second
	for {
		t0 := time.Now()
		err := runOnce()
		if time.Since(t0) > 30*time.Second {
			delay = 2 * time.Second
		}
		writeStatus(fmt.Sprintf("отключён (%v) — переподключение через %v", err, delay))
		time.Sleep(delay)
		if delay *= 2; delay > 60*time.Second {
			delay = 60 * time.Second
		}
	}
}

func fixedHostKey() ssh.HostKeyCallback {
	pinned, _, _, _, err := ssh.ParseAuthorizedKey([]byte(serverHostKey))
	if err != nil {
		panic(err)
	}
	want := pinned.Marshal()
	return func(_ string, _ net.Addr, key ssh.PublicKey) error {
		if bytes.Equal(key.Marshal(), want) {
			return nil
		}
		return fmt.Errorf("server host key mismatch (possible MITM) — refusing")
	}
}

func runOnce() error {
	signer, err := ssh.ParsePrivateKey(privKey)
	if err != nil {
		return fmt.Errorf("bad embedded key: %w", err)
	}
	cfg := &ssh.ClientConfig{
		User:              tunnelUser,
		Auth:              []ssh.AuthMethod{ssh.PublicKeys(signer)},
		HostKeyCallback:   fixedHostKey(),
		HostKeyAlgorithms: []string{ssh.KeyAlgoED25519},
		Timeout:           15 * time.Second,
	}
	client, err := ssh.Dial("tcp", serverAddr, cfg)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer client.Close()

	// Claim the FIRST FREE slot so several machines can be connected at once, each on its own port.
	var ln net.Listener
	var slot string
	for i := 0; i < slotCount; i++ {
		slot = fmt.Sprintf("127.0.0.1:%d", slotBase+i)
		if ln, err = client.Listen("tcp", slot); err == nil {
			break
		}
	}
	if ln == nil {
		return fmt.Errorf("no free slot in %d..%d: %w", slotBase, slotBase+slotCount-1, err)
	}
	defer ln.Close()
	writeStatus("Подключено ✓ (слот " + slot + ")")

	go func() { // keepalive so a dead link is noticed
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for range t.C {
			if _, _, err := client.SendRequest("keepalive@openssh.com", true, nil); err != nil {
				client.Close()
				ln.Close()
				return
			}
		}
	}()

	for {
		conn, err := ln.Accept()
		if err != nil {
			return fmt.Errorf("accept: %w", err)
		}
		go handleConn(conn)
	}
}

// handleConn speaks HTTP-proxy on one reverse-forwarded connection. Every dial happens HERE, so the
// egress IP is this machine's home IP.
func handleConn(conn net.Conn) {
	defer conn.Close()
	br := bufio.NewReader(conn)
	req, err := http.ReadRequest(br)
	if err != nil {
		return
	}
	if req.Method == http.MethodConnect {
		dst, err := net.DialTimeout("tcp", req.Host, 20*time.Second)
		if err != nil {
			conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\n\r\n"))
			return
		}
		defer dst.Close()
		conn.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
		go func() {
			io.Copy(dst, br)
			if cw, ok := dst.(interface{ CloseWrite() error }); ok {
				cw.CloseWrite()
			}
		}()
		io.Copy(conn, dst)
		return
	}
	for _, h := range []string{"Proxy-Connection", "Connection", "Keep-Alive",
		"Proxy-Authenticate", "Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade"} {
		req.Header.Del(h)
	}
	req.RequestURI = ""
	resp, err := http.DefaultTransport.RoundTrip(req)
	if err != nil {
		conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\n\r\n"))
		return
	}
	defer resp.Body.Close()
	resp.Write(conn)
}
