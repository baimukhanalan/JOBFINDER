// JobFinder residential proxy — single self-contained client.
//
// Modes:
//   (no args)     first run: copy self into place, register autostart, start hidden in background, notify
//   --run         the background supervisor: hold a reverse tunnel over 443/WSS and expose a loopback SOCKS slot
//   --uninstall   stop + remove autostart (turn it off cleanly)
//
// The server egresses to the internet THROUGH this machine's home connection (residential IP) so it can
// reach sites that block the datacenter. Transport is chisel over HTTPS/WebSocket to proxy.systeam.kz:443
// (survives networks that block outbound SSH/:22). The server side binds ONE loopback slot in 8120..8129
// (one per connected machine) — never public. Reconnects with exponential backoff, forever.
//
// Honest status: "Подключено ✓" is written ONLY after the server confirms it bound the reverse slot
// (chisel's "Connected" line is emitted only after the server-side listen succeeds — a busy or denied
// slot never reaches it). There is no local listener that comes up independently of the server, so a
// blocked network can never show a false "connected".
//
// The shared tunnel credential is injected at build time via -ldflags "-X main.chiselAuth=..." (kept out
// of git). The server key fingerprint is pinned below (public) so the client can't be MITM'd even if TLS
// were somehow subverted.
package main

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	chclient "github.com/jpillora/chisel/client"
)

// chiselAuth is injected at build time (-ldflags "-X main.chiselAuth=user:pass"); empty in source.
var chiselAuth string

const (
	serverURL = "https://proxy.systeam.kz/link"                    // chisel WSS endpoint behind nginx on 443
	chiselFP  = "/GVgVoPYa802RrE/HwRWH8SKCCM+i5L0w0WFXCUuqVE="     // pinned server key fingerprint (public)
	slotBase  = 8120                                               // loopback slot range 8120..8120+slotCount-1
	slotCount = 10
	taskName  = "JobFinderResidentialProxy"
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

// tunState is updated by the log scanner from chisel's own output; the connect loop reads it to decide
// honest status + which slot to claim. Reset before every attempt.
type tunState struct {
	mu        sync.Mutex
	connected bool   // saw "Connected (Latency ...)" — the server bound the slot (real round-trip)
	busy      bool   // saw "Server cannot listen" — the slot is taken by another machine
	denied    bool   // saw "... denied" — credential/slot not permitted (server misconfig)
	lastErr   string // last connection error line
}

func (s *tunState) reset() {
	s.mu.Lock()
	s.connected, s.busy, s.denied, s.lastErr = false, false, false, ""
	s.mu.Unlock()
}

func (s *tunState) snap() (bool, bool, bool, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connected, s.busy, s.denied, s.lastErr
}

// scan reads chisel's log stream and translates the meaningful lines into tunState transitions.
func scan(r *os.File, s *tunState) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := sc.Text()
		s.mu.Lock()
		switch {
		case strings.Contains(line, "Connected (Latency"):
			s.connected = true
		case strings.Contains(line, "Server cannot listen"):
			s.busy = true
		case strings.Contains(line, "denied"):
			s.denied, s.lastErr = true, line
		case strings.Contains(line, "Disconnected"):
			s.connected = false
		case strings.Contains(line, "Connection error"):
			s.lastErr = line
		}
		s.mu.Unlock()
	}
}

func supervise() {
	// Route chisel's logger (which binds os.Stderr at NewClient time) into a pipe we parse for honest
	// status. Set BEFORE any NewClient call so every client logs into it.
	pr, pw, err := os.Pipe()
	if err == nil {
		os.Stderr = pw
	}
	st := &tunState{}
	if pr != nil {
		go scan(pr, st)
	}

	notified := false
	backoff := 2 * time.Second
	for {
		slot, connected := connectCycle(st, &notified)
		if connected {
			backoff = 2 * time.Second // a real session held; reconnect promptly on drop
			writeStatus("соединение потеряно — переподключаюсь…")
			time.Sleep(backoff)
		} else {
			_ = slot
			writeStatus(fmt.Sprintf("нет соединения — проверьте интернет. Повтор через %v", backoff))
			time.Sleep(backoff)
			if backoff *= 2; backoff > 60*time.Second {
				backoff = 60 * time.Second
			}
		}
	}
}

// connectCycle tries slots 8120..8129 in order. It returns (slot, true) after a slot connected AND the
// tunnel later dropped (so the caller reconnects), or (slot, false) when no slot could connect this pass
// (all busy, or a real network/credential error) so the caller backs off.
func connectCycle(st *tunState, notified *bool) (int, bool) {
	for i := 0; i < slotCount; i++ {
		slot := slotBase + i
		st.reset()
		writeStatus(fmt.Sprintf("подключаюсь… (слот %d)", slot))

		cfg := &chclient.Config{
			Server:           serverURL,
			Auth:             chiselAuth,
			Fingerprint:      chiselFP,
			KeepAlive:        25 * time.Second,
			MaxRetryCount:    0, // fail fast — WE own slot-cycling + backoff in supervise
			MaxRetryInterval: 60 * time.Second,
			Remotes:          []string{fmt.Sprintf("R:127.0.0.1:%d:socks", slot)},
		}
		c, err := chclient.NewClient(cfg)
		if err != nil {
			continue
		}
		ctx, cancel := context.WithCancel(context.Background())
		if err := c.Start(ctx); err != nil {
			cancel()
			continue
		}

		switch waitOutcome(st, 20*time.Second) {
		case "connected":
			writeStatus(fmt.Sprintf("Подключено ✓ (слот %d)", slot))
			if !*notified {
				*notified = true
				notify("JobFinder residential proxy", "Подключено ✓\nСервер ходит через твой домашний IP.\nМожно закрыть это окно — работает в фоне.")
			}
			c.Wait() // block until the tunnel drops
			cancel()
			return slot, true
		case "busy":
			cancel()
			c.Wait()
			continue // slot taken by another machine — try the next one immediately
		default: // "error" or timeout
			cancel()
			c.Wait()
			return slot, false // real failure — let supervise back off
		}
	}
	return 0, false
}

// waitOutcome polls the scanner-fed state until we know how this attempt went.
func waitOutcome(st *tunState, timeout time.Duration) string {
	deadline := time.Now().Add(timeout)
	for {
		connected, busy, denied, _ := st.snap()
		switch {
		case connected:
			return "connected"
		case busy:
			return "busy"
		case denied:
			return "error"
		case time.Now().After(deadline):
			return "error"
		}
		time.Sleep(150 * time.Millisecond)
	}
}
