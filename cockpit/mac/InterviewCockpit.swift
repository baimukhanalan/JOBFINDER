// Interview Cockpit — native macOS WINDOWED app for interviewer Alan Bai, JobFinder-styled.
//
// A real .app (Dock + visible window) whose UI is a WKWebView rendering a local HTML page in
// JobFinder's visual language (royal-blue #0c47c2, cards, system font). It reads the server-staged
// ~/NativelyInbox/next.json and, on «Подготовить сейчас» (or ~20 min before the собес), makes the
// laptop sit-down-and-go:
//   (a) keep the Mac awake (caffeinate -dimsu),
//   (b) open Natively (its résumé-watcher loads THAT candidate's résumé active),
//   (c) open GOOGLE CHROME directly on the candidate's Zoom/Teams/Meet room (join_url) — the owner
//       just clicks «Подключиться», which raises that Chrome tab.
// JS→native via WKScriptMessageHandler. Single-instance; never kills Natively; never touches OBS/CDP.
import AppKit
import WebKit
import Foundation

let HOME = FileManager.default.homeDirectoryForCurrentUser
let INBOX = HOME.appendingPathComponent("NativelyInbox")
let MANIFEST = INBOX.appendingPathComponent("next.json")
let MARKER = INBOX.appendingPathComponent(".cockpit_native_prepped")
let LOGFILE = INBOX.appendingPathComponent("cockpit_native.log")
let PREP_LEAD: TimeInterval = 20 * 60
let KEEP_AWAKE_SECS = 5400

func logLine(_ s: String) {
    let line = "\(ISO8601DateFormatter().string(from: Date())) \(s)\n"
    guard let data = line.data(using: .utf8) else { return }
    if FileManager.default.fileExists(atPath: LOGFILE.path), let h = try? FileHandle(forWritingTo: LOGFILE) {
        defer { try? h.close() }; _ = try? h.seekToEnd(); try? h.write(contentsOf: data)
    } else { try? data.write(to: LOGFILE) }
}
func lastLogLine() -> String {
    guard let s = try? String(contentsOf: LOGFILE, encoding: .utf8) else { return "" }
    return s.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
}

struct Plan {
    var mailbox = "", name = "", company = "", startRaw = ""
    var startTs: Date?
    var resumeFound = false
    var joinUrl: String?, bookingUrl: String?
    var cockpitUrl = "https://jobs.systeam.kz/cabinet"
}
func str(_ v: Any?) -> String? { if let s = v as? String, !s.isEmpty { return s }; return nil }

func readPlan() -> Plan? {
    guard let data = try? Data(contentsOf: MANIFEST),
          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
    var p = Plan()
    p.mailbox = str(obj["mailbox"]) ?? ""
    p.name = str(obj["candidate_name"]) ?? p.mailbox
    p.company = str(obj["company"]) ?? ""
    p.startRaw = str(obj["start_ts"]) ?? ""
    p.resumeFound = (obj["has_resume"] as? Bool) ?? false
    p.joinUrl = str(obj["join_url"]); p.bookingUrl = str(obj["booking_url"])
    p.cockpitUrl = str(obj["cockpit_url"]) ?? p.cockpitUrl
    if !p.startRaw.isEmpty {
        let f1 = ISO8601DateFormatter(); f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let f2 = ISO8601DateFormatter(); f2.formatOptions = [.withInternetDateTime]
        p.startTs = f1.date(from: p.startRaw) ?? f2.date(from: p.startRaw)
    }
    return p.mailbox.isEmpty ? nil : p
}

func spawn(_ path: String, _ args: [String]) {
    let proc = Process(); proc.executableURL = URL(fileURLWithPath: path); proc.arguments = args
    try? proc.run()
}
let CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
let CHROME_PROFILE = HOME.appendingPathComponent("Library/NativelyCockpit/chrome").path
let DEBUG_PORT = 9222

// Open `url` in a DEDICATED, controllable Chrome instance (its own clean profile + a loopback-only
// remote-debugging port) — never touches the user's main 33-tab Chrome, and the landed tab is
// verifiable via http://127.0.0.1:9222/json. Re-launching with the SAME --user-data-dir reuses the
// running instance (focuses it + adds/opens the tab) instead of duplicating.
func openInChrome(_ url: String) {
    if FileManager.default.fileExists(atPath: CHROME_BIN) {
        spawn(CHROME_BIN, ["--user-data-dir=\(CHROME_PROFILE)",
                           "--remote-debugging-port=\(DEBUG_PORT)",
                           "--no-first-run", "--no-default-browser-check", url])
    } else {
        spawn("/usr/bin/open", [url])   // fallback: default browser
    }
}

func shellCapture(_ path: String, _ args: [String]) -> String {
    let p = Process(); p.executableURL = URL(fileURLWithPath: path); p.arguments = args
    let out = Pipe(); p.standardOutput = out; p.standardError = Pipe()
    do { try p.run() } catch { return "" }
    let data = out.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
    return String(data: data, encoding: .utf8) ?? ""
}

// Poll the dedicated Chrome's loopback /json and LOG whether a tab really landed on `needle`
// (so cockpit_native.log proves the Zoom room opened — not just that a process was spawned).
func verifyChromeTab(_ needle: String) {
    DispatchQueue.global().async {
        for _ in 0..<10 {
            let j = shellCapture("/usr/bin/curl", ["-s", "--max-time", "2",
                                                   "http://127.0.0.1:\(DEBUG_PORT)/json"])
            if j.contains(needle) { logLine("VERIFY ok — dedicated Chrome tab on \(needle)"); return }
            Thread.sleep(forTimeInterval: 1.2)
        }
        logLine("VERIFY fail — no dedicated Chrome tab on \(needle) after 10 tries")
    }
}
func targetURL(_ p: Plan) -> String { p.joinUrl ?? p.bookingUrl ?? p.cockpitUrl }
func markerKey(_ p: Plan) -> String { "\(p.mailbox)|\(p.startRaw)" }
func alreadyPrepped(_ p: Plan) -> Bool {
    (try? String(contentsOf: MARKER, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) == markerKey(p)
}
func prepare(_ p: Plan, manual: Bool) {
    let url = targetURL(p)
    spawn("/usr/bin/caffeinate", ["-dimsu", "-t", "\(KEEP_AWAKE_SECS)"])
    spawn("/usr/bin/open", ["-a", "Natively"])
    openInChrome(url)
    try? markerKey(p).data(using: .utf8)?.write(to: MARKER)
    logLine("PREP(\(manual ? "manual" : "auto")) \(p.name) · \(p.company) -> \(url)")
    let needle = url.firstIndex(of: "?").map { String(url[..<$0]) } ?? url   // room URL sans ?pwd query
    verifyChromeTab(needle)
}
func fmtWhen(_ d: Date?) -> String {
    guard let d = d else { return "время не назначено" }
    let f = DateFormatter(); f.dateFormat = "dd.MM HH:mm"; f.timeZone = TimeZone.current
    return f.string(from: d)
}
func esc(_ s: String) -> String {
    s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
     .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
}

func pageHTML(_ plan: Plan?) -> String {
    let css = """
    :root{--accent:#0c47c2;--accent-ink:#fff;--bg:#f4f5f7;--panel:#fff;--ink:#17182b;
      --ink-soft:#5b6072;--line:#e4e7ee;--ok:#1a7f4b;--r:16px;--r-sm:11px;}
    *{box-sizing:border-box;} html,body{margin:0;height:100%;}
    body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Segoe UI,Roboto,sans-serif;
      background:var(--bg);color:var(--ink);padding:22px;-webkit-user-select:none;}
    .brand{display:flex;align-items:center;gap:10px;margin:0 0 16px;}
    .logo{width:30px;height:30px;border-radius:9px;background:var(--accent);color:#fff;font-weight:800;
      display:flex;align-items:center;justify-content:center;font-family:Georgia,serif;}
    .brand b{font-size:15px;letter-spacing:-.01em;} .brand span{color:var(--ink-soft);font-size:12.5px;}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
      padding:20px;margin:0 0 14px;box-shadow:0 1px 2px rgba(20,25,50,.04);}
    .nm{font-size:20px;font-weight:800;letter-spacing:-.02em;margin:0 0 4px;}
    .meta{color:var(--ink-soft);font-size:13.5px;margin:0 0 14px;}
    .row{display:flex;gap:9px;padding:7px 0;border-top:1px solid var(--line);font-size:13.5px;}
    .row:first-of-type{border-top:0;} .k{flex:0 0 120px;color:var(--ink-soft);font-weight:600;}
    .v{color:var(--ink);word-break:break-word;} .v a{color:var(--accent);text-decoration:none;}
    .ok{color:var(--ok);font-weight:700;} .warn{color:#b4690e;font-weight:700;}
    .btns{display:flex;flex-wrap:wrap;gap:10px;margin-top:4px;}
    button{font:600 14px/1 inherit;min-height:46px;padding:0 20px;border-radius:12px;border:1px solid var(--line);
      background:var(--panel);color:var(--ink);cursor:pointer;transition:.12s;}
    button:hover{border-color:#c7ccd8;} button:active{transform:translateY(1px);}
    button.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
    button.go{background:var(--ok);color:#fff;border-color:var(--ok);}
    button:disabled{opacity:.45;cursor:default;}
    .status{margin-top:14px;color:var(--ink-soft);font-size:12.5px;}
    .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;}
    .pill.ok{background:#e7f4ec;color:var(--ok);} .pill.wait{background:#fff3e0;color:#b4690e;}
    """
    let body: String
    if let p = plan {
        let url = p.joinUrl ?? p.bookingUrl
        let prepped = alreadyPrepped(p)
        let resumeRow = p.resumeFound ? "<span class='ok'>готово</span>"
            : "<span class='warn'>не найдено</span>"
        let linkRow = url.map { "<a href='#' onclick=\"send('connect');return false\">\(esc($0))</a>" }
            ?? "<span class='warn'>ссылка на созвон не указана</span>"
        let state = prepped ? "<span class='pill ok'>✓ готово к собесу</span>"
            : "<span class='pill wait'>ожидает подготовки</span>"
        body = """
        <div class='card'>
          <div class='nm'>\(esc(p.name))</div>
          <div class='meta'>\(state)</div>
          <div class='row'><span class='k'>Компания</span><span class='v'>\(esc(p.company.isEmpty ? "—" : p.company))</span></div>
          <div class='row'><span class='k'>Время</span><span class='v'>\(esc(fmtWhen(p.startTs)))</span></div>
          <div class='row'><span class='k'>Резюме</span><span class='v'>\(resumeRow)</span></div>
          <div class='row'><span class='k'>Созвон</span><span class='v'>\(linkRow)</span></div>
        </div>
        <div class='btns'>
          <button class='primary' onclick="send('prep')">Подготовить сейчас</button>
          <button class='go' onclick="send('connect')" \(url == nil ? "disabled" : "")>Подключиться</button>
          <button onclick="send('schedule')">Открыть расписание</button>
        </div>
        <div class='status'>\(esc(lastLogLine()))</div>
        """
    } else {
        body = """
        <div class='card'><div class='nm'>Нет предстоящих собесов</div>
        <div class='meta'>Когда появится запись — она отобразится здесь автоматически.</div></div>
        <div class='btns'><button onclick="send('schedule')">Открыть расписание</button></div>
        """
    }
    return """
    <!doctype html><html lang='ru'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><style>\(css)</style></head>
    <body>
      <div class='brand'><span class='logo'>JF</span><div><b>Interview Cockpit</b><br><span>Предстоящий собес</span></div></div>
      \(body)
      <script>function send(a){window.webkit.messageHandlers.cockpit.postMessage({action:a});}</script>
    </body></html>
    """
}

final class Cockpit: NSObject, NSApplicationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!
    var plan: Plan?

    func applicationDidFinishLaunching(_ note: Notification) {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "cockpit")
        web = WKWebView(frame: .zero, configuration: cfg)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 480),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Interview Cockpit"
        window.contentView = web
        window.center(); window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.tick() }
        tick()
        NSApp.activate(ignoringOtherApps: true)
        logLine("Interview Cockpit (webview) started")
    }

    func render() { web.loadHTMLString(pageHTML(plan), baseURL: nil) }

    func tick() {
        plan = readPlan(); render()
        guard let p = plan, let st = p.startTs else { return }
        let delta = st.timeIntervalSinceNow
        if delta <= PREP_LEAD && delta > -3600 && !alreadyPrepped(p) { prepare(p, manual: false); render() }
    }

    // JS → native
    func userContentController(_ u: WKUserContentController, didReceive msg: WKScriptMessage) {
        guard let d = msg.body as? [String: Any], let action = d["action"] as? String else { return }
        switch action {
        case "prep":   if let p = plan { prepare(p, manual: true); render() }
        case "connect":
            if let p = plan { openInChrome(p.joinUrl ?? p.bookingUrl ?? p.cockpitUrl); logLine("CONNECT -> \(p.joinUrl ?? p.bookingUrl ?? p.cockpitUrl)") }
        case "schedule":
            openInChrome(plan?.cockpitUrl ?? "https://jobs.systeam.kz/cabinet"); logLine("SCHEDULE opened")
        default: break
        }
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return true
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
}

// single-instance: a second launch re-focuses the running window, then exits
let myID = Bundle.main.bundleIdentifier ?? "com.jobfinder.cockpit.native"
let others = NSRunningApplication.runningApplications(withBundleIdentifier: myID)
    .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
if let running = others.first { running.activate(options: [.activateAllWindows]); exit(0) }

let app = NSApplication.shared
let delegate = Cockpit()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
