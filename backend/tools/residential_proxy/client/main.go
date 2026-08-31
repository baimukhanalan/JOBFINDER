// JobFinder residential proxy — single self-contained client.
//
// Modes:
//   (no args)     first run: copy self into place, register autostart, START a living background
//                 process immediately, notify
//   --run         the background supervisor: hold a reverse tunnel over 443/WSS and expose a loopback
//                 SOCKS slot
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
// EVERYTHING is logged to a fixed file from the very first line of main (jobfinder-proxy.log in the
// user's home) so a silent crash on a real machine still leaves evidence. The debug build
// (-X main.debugMode=1, win-debug.exe) additionally has a console, runs in the FOREGROUND, and tees
// every step to the console so a failure is visible — the window stays open (the process keeps
// running / retrying) so it can be screenshotted.
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

// debugMode is "1" in the console debug build (win-debug.exe) — foreground + verbose + console tee.
var debugMode string

// origStderr is the real console (if any) captured BEFORE supervise redirects os.Stderr into a pipe.
var origStderr = os.Stderr

const (
	serverURL = "https://proxy.systeam.kz/link"                // chisel WSS endpoint behind nginx on 443
	chiselFP  = "/GVgVoPYa802RrE/HwRWH8SKCCM+i5L0w0WFXCUuqVE=" // pinned server key fingerprint (public)
	slotBase  = 8120                                           // loopback slot range 8120..8120+slotCount-1
	slotCount = 10
	taskName  = "JobFinderResidentialProxy"
)

func isDebug() bool { return debugMode == "1" }

func argMode() string {
	if len(os.Args) > 1 {
		return os.Args[1]
	}
	return ""
}

func main() {
	// Log FIRST THING and create the data dir before anything can fail, so even a silent crash leaves a
	// trace on the machine. Recover from any panic into the log (+ pause in debug so it can be read).
	os.MkdirAll(installDir(), 0755)
	rotateLogIfBig()
	defer func() {
		if r := recover(); r != nil {
			logf("PANIC: %v", r)
			if isDebug() {
				pauseConsole()
			}
		}
	}()
	exe, _ := os.Executable()
	logf("=== agent start: mode=%q os=%s/%s debug=%q ===", argMode(), runtime.GOOS, runtime.GOARCH, debugMode)
	logf("exe=%s  installDir=%s  server=%s  log=%s", exe, installDir(), serverURL, logPath())

	// The debug build ALWAYS runs the tunnel in the foreground with verbose output, regardless of args,
	// so a double-click shows every step and the window stays open (supervise loops forever).
	if isDebug() {
		logf("DEBUG build — running the tunnel in the FOREGROUND. Это окно остаётся открытым; закрой его чтобы остановить.")
		fmt.Fprintln(origStderr, "JobFinder residential proxy — ДИАГНОСТИКА. Смотри строки ниже.")
		fmt.Fprintln(origStderr, "Лог также пишется в:", logPath())
		fmt.Fprintln(origStderr, "")
		supervise() // never returns
		return
	}

	switch argMode() {
	case "--run":
		supervise()
	case "--uninstall":
		doUninstall()
	default:
		doInstall()
	}
}

// ---- logging -----------------------------------------------------------------------------------

func logPath() string {
	name := "jobfinder-proxy.log"
	if isDebug() {
		name = "jobfinder-proxy-debug.log"
	}
	base := os.Getenv("USERPROFILE") // Windows
	if base == "" {
		base = os.Getenv("HOME")
	}
	if base == "" {
		base = installDir()
	}
	return filepath.Join(base, name)
}

var logMu sync.Mutex

func logf(format string, a ...any) {
	line := time.Now().Format("2006-01-02 15:04:05") + "  " + fmt.Sprintf(format, a...)
	logMu.Lock()
	if f, err := os.OpenFile(logPath(), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0644); err == nil {
		f.WriteString(line + "\n")
		f.Close()
	}
	logMu.Unlock()
	if isDebug() && origStderr != nil {
		fmt.Fprintln(origStderr, line)
	}
}

// rotateLogIfBig truncates the log once it passes ~512 KB so a months-long reconnect history can't grow
// unbounded on the user's machine.
func rotateLogIfBig() {
	if fi, err := os.Stat(logPath()); err == nil && fi.Size() > 512*1024 {
		os.WriteFile(logPath(), []byte(""), 0644)
	}
}

func pauseConsole() {
	if origStderr != nil {
		fmt.Fprintln(origStderr, "\n[нажми Enter чтобы закрыть это окно]")
	}
	bufio.NewReader(os.Stdin).ReadString('\n')
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
			if err := os.WriteFile(ap, data, 0755); err != nil {
				logf("copy self -> %s FAILED: %v", ap, err)
			} else {
				logf("copied self -> %s (%d bytes)", ap, len(data))
			}
		} else {
			logf("read self %s FAILED: %v", self, err)
		}
	}

	// (1) START A LIVING BACKGROUND PROCESS IMMEDIATELY, independent of any logon task, so the tunnel
	// comes up now (the old code relied on `schtasks /Run`, which fails silently) and survives this
	// installer exiting.
	if err := startDetached(ap); err != nil {
		logf("immediate background --run start FAILED: %v", err)
	} else {
		logf("immediate background --run spawned")
	}

	// (2) Register autostart so it also comes back after a reboot/logon.
	switch runtime.GOOS {
	case "windows":
		tr := "\"" + ap + "\" --run"
		out, err := exec.Command("schtasks", "/Create", "/TN", taskName, "/TR", tr,
			"/SC", "ONLOGON", "/RL", "LIMITED", "/F").CombinedOutput()
		logf("schtasks /Create rc=%v out=%q", err, strings.TrimSpace(string(out)))
	case "darwin":
		writePlist(ap)
		exec.Command("launchctl", "unload", plistPath()).Run()
		out, err := exec.Command("launchctl", "load", plistPath()).CombinedOutput()
		logf("launchctl load rc=%v out=%q", err, strings.TrimSpace(string(out)))
	default:
		// linux: startDetached above already launched it; no login manager assumed.
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
		exec.Command("taskkill", "/F", "/IM", "agent.exe").Run()
	case "darwin":
		exec.Command("launchctl", "unload", plistPath()).Run()
		os.Remove(plistPath())
	default:
		exec.Command("pkill", "-f", agentPath()).Run()
	}
	writeStatus("отключено")
	logf("uninstalled")
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

// scan reads chisel's log stream, TEES every line to our log file (and the console in debug), and
// translates the meaningful lines into tunState transitions.
func scan(r *os.File, s *tunState) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for sc.Scan() {
		line := sc.Text()
		logf("chisel: %s", line) // verbose evidence — every step chisel reports
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
	// status + tee to the log. Set BEFORE any NewClient call so every client logs into it.
	pr, pw, err := os.Pipe()
	if err == nil {
		os.Stderr = pw
	} else {
		logf("os.Pipe FAILED (chisel logs will not be captured): %v", err)
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
			logf("session on slot %d dropped — reconnecting", slot)
			writeStatus("соединение потеряно — переподключаюсь…")
			time.Sleep(backoff)
		} else {
			logf("no slot connected this pass — retry in %v", backoff)
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
		logf("trying slot %d — dialing %s", slot, serverURL)
		writeStatus(fmt.Sprintf("подключаюсь… (слот %d)", slot))

		cfg := &chclient.Config{
			Server:           serverURL,
			Auth:             chiselAuth,
			Fingerprint:      chiselFP,
			KeepAlive:        25 * time.Second,
			MaxRetryCount:    0, // fail fast — WE own slot-cycling + backoff in supervise
			MaxRetryInterval: 60 * time.Second,
			Verbose:          isDebug(), // chisel Debugf lines too, in the debug build
			Remotes:          []string{fmt.Sprintf("R:127.0.0.1:%d:socks", slot)},
		}
		c, err := chclient.NewClient(cfg)
		if err != nil {
			logf("slot %d NewClient error: %v", slot, err)
			continue
		}
		ctx, cancel := context.WithCancel(context.Background())
		if err := c.Start(ctx); err != nil {
			logf("slot %d Start error: %v", slot, err)
			cancel()
			continue
		}

		switch waitOutcome(st, 25*time.Second) {
		case "connected":
			logf("slot %d CONNECTED — server bound the reverse forward", slot)
			writeStatus(fmt.Sprintf("Подключено ✓ (слот %d)", slot))
			if !*notified {
				*notified = true
				notify("JobFinder residential proxy", "Подключено ✓\nСервер ходит через твой домашний IP.\nМожно закрыть это окно — работает в фоне.")
			}
			c.Wait() // block until the tunnel drops
			cancel()
			return slot, true
		case "busy":
			_, _, _, _ = st.snap()
			logf("slot %d busy (taken by another machine) — next slot", slot)
			cancel()
			c.Wait()
			continue // slot taken — try the next one immediately
		default: // "error" or timeout
			_, _, _, le := st.snap()
			logf("slot %d did NOT connect: %s", slot, le)
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
