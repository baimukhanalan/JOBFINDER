// Interview Cockpit — native macOS WINDOWED app for interviewer Alan Bai.
//
// A real .app with a Dock icon and a VISIBLE main window laying out the upcoming собес / hiring event
// (NOT a menu-bar-only agent, NOT a browser-URL shortcut). It reads the server-staged manifest
// ~/NativelyInbox/next.json (written by backend.interviews.cockpit_prep) and ~20 min before the собес
// (or on «Подготовить сейчас») makes the laptop sit-down-and-go:
//   (a) keeps the Mac awake (caffeinate -dimsu),
//   (b) opens Natively (its cockpit résumé-watcher auto-loads THAT candidate's résumé),
//   (c) opens the default browser at the candidate's Zoom/Teams/Meet room (join_url), else the
//       recruiter booking link, else the cockpit schedule.
// Single-instance (a second launch just re-focuses the running window). Never kills Natively, never
// touches OBS/camera/CDP.
import AppKit
import Foundation

let HOME = FileManager.default.homeDirectoryForCurrentUser
let INBOX = HOME.appendingPathComponent("NativelyInbox")
let MANIFEST = INBOX.appendingPathComponent("next.json")
let MARKER = INBOX.appendingPathComponent(".cockpit_native_prepped")
let LOGFILE = INBOX.appendingPathComponent("cockpit_native.log")
let PREP_LEAD: TimeInterval = 20 * 60      // begin preparing 20 min before the собес
let KEEP_AWAKE_SECS = 5400                 // 90-min awake window per prep

func logLine(_ s: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    let line = "\(ts) \(s)\n"
    guard let data = line.data(using: .utf8) else { return }
    if FileManager.default.fileExists(atPath: LOGFILE.path), let h = try? FileHandle(forWritingTo: LOGFILE) {
        defer { try? h.close() }
        _ = try? h.seekToEnd(); try? h.write(contentsOf: data)
    } else {
        try? data.write(to: LOGFILE)
    }
}

func lastLogLine() -> String {
    guard let s = try? String(contentsOf: LOGFILE, encoding: .utf8) else { return "" }
    return s.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
}

struct Plan {
    var mailbox = ""
    var name = ""
    var company = ""
    var startRaw = ""
    var startTs: Date?
    var resumeFound = false
    var joinUrl: String?
    var bookingUrl: String?
    var cockpitUrl = "https://jobs.systeam.kz/cabinet"
}

func str(_ v: Any?) -> String? {
    if let s = v as? String, !s.isEmpty { return s }
    return nil
}

func readPlan() -> Plan? {
    guard let data = try? Data(contentsOf: MANIFEST),
          let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
    var p = Plan()
    p.mailbox = str(obj["mailbox"]) ?? ""
    p.name = str(obj["candidate_name"]) ?? p.mailbox
    p.company = str(obj["company"]) ?? ""
    p.startRaw = str(obj["start_ts"]) ?? ""
    p.resumeFound = (obj["has_resume"] as? Bool) ?? false
    p.joinUrl = str(obj["join_url"])
    p.bookingUrl = str(obj["booking_url"])
    p.cockpitUrl = str(obj["cockpit_url"]) ?? p.cockpitUrl
    if !p.startRaw.isEmpty {
        let f1 = ISO8601DateFormatter(); f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let f2 = ISO8601DateFormatter(); f2.formatOptions = [.withInternetDateTime]
        p.startTs = f1.date(from: p.startRaw) ?? f2.date(from: p.startRaw)
    }
    return p.mailbox.isEmpty ? nil : p
}

func spawn(_ path: String, _ args: [String]) {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: path)
    proc.arguments = args
    try? proc.run()
}

func targetURL(_ p: Plan) -> String { p.joinUrl ?? p.bookingUrl ?? p.cockpitUrl }
func markerKey(_ p: Plan) -> String { "\(p.mailbox)|\(p.startRaw)" }
func alreadyPrepped(_ p: Plan) -> Bool {
    let cur = (try? String(contentsOf: MARKER, encoding: .utf8))?
        .trimmingCharacters(in: .whitespacesAndNewlines)
    return cur == markerKey(p)
}

func prepare(_ p: Plan, manual: Bool) {
    spawn("/usr/bin/caffeinate", ["-dimsu", "-t", "\(KEEP_AWAKE_SECS)"])   // keep the Mac awake
    spawn("/usr/bin/open", ["-a", "Natively"])                            // watcher auto-loads résumé
    spawn("/usr/bin/open", [targetURL(p)])                                // browser at Zoom/booking
    try? markerKey(p).data(using: .utf8)?.write(to: MARKER)
    logLine("PREP(\(manual ? "manual" : "auto")) \(p.name) · \(p.company) -> \(targetURL(p))")
}

func fmtWhen(_ d: Date?) -> String {
    guard let d = d else { return "время не назначено" }
    let f = DateFormatter(); f.dateFormat = "dd.MM HH:mm"; f.timeZone = TimeZone.current
    return f.string(from: d)
}

final class Cockpit: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var plan: Plan?
    let nameL = NSTextField(labelWithString: "")
    let metaL = NSTextField(labelWithString: "")
    let resumeL = NSTextField(labelWithString: "")
    let linkL = NSTextField(labelWithString: "")
    let statusL = NSTextField(labelWithString: "")
    let prepBtn = NSButton(title: "Подготовить сейчас", target: nil, action: nil)
    let schedBtn = NSButton(title: "Открыть расписание", target: nil, action: nil)
    let linkBtn = NSButton(title: "Открыть ссылку", target: nil, action: nil)

    func applicationDidFinishLaunching(_ note: Notification) {
        buildWindow()
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.tick() }
        tick()
        NSApp.activate(ignoringOtherApps: true)
        logLine("Interview Cockpit (windowed) started")
    }

    func buildWindow() {
        let heading = NSTextField(labelWithString: "Предстоящий собес")
        heading.font = NSFont.boldSystemFont(ofSize: 20)
        nameL.font = NSFont.boldSystemFont(ofSize: 17)
        metaL.font = NSFont.systemFont(ofSize: 14)
        resumeL.font = NSFont.systemFont(ofSize: 13)
        linkL.font = NSFont.systemFont(ofSize: 12); linkL.textColor = .secondaryLabelColor
        linkL.lineBreakMode = .byTruncatingMiddle; linkL.maximumNumberOfLines = 1
        statusL.font = NSFont.systemFont(ofSize: 12); statusL.textColor = .secondaryLabelColor
        statusL.lineBreakMode = .byWordWrapping; statusL.maximumNumberOfLines = 3
        for l in [heading, nameL, metaL, resumeL, linkL, statusL] { l.isSelectable = true }

        prepBtn.target = self; prepBtn.action = #selector(doPrep)
        prepBtn.bezelStyle = .rounded; prepBtn.keyEquivalent = "\r"
        schedBtn.target = self; schedBtn.action = #selector(openSchedule); schedBtn.bezelStyle = .rounded
        linkBtn.target = self; linkBtn.action = #selector(openLink); linkBtn.bezelStyle = .rounded

        let btns = NSStackView(views: [prepBtn, schedBtn, linkBtn])
        btns.orientation = .horizontal; btns.spacing = 10

        let stack = NSStackView(views: [heading, nameL, metaL, resumeL, linkL, btns, statusL])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor),
        ])

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 400),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Interview Cockpit"
        window.contentView = content
        window.center()
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
    }

    func tick() {
        plan = readPlan()
        refresh()
        guard let p = plan, let st = p.startTs else { return }
        let delta = st.timeIntervalSinceNow
        if delta <= PREP_LEAD && delta > -3600 && !alreadyPrepped(p) {
            prepare(p, manual: false); refresh()
        }
    }

    func refresh() {
        if let p = plan {
            nameL.stringValue = "👤 \(p.name)"
            metaL.stringValue = "🏢 \(p.company.isEmpty ? "—" : p.company)      🕒 \(fmtWhen(p.startTs))"
            resumeL.stringValue = "📄 Резюме: " + (p.resumeFound ? "готово" : "не найдено (старый бэклог)")
            let url = p.joinUrl ?? p.bookingUrl
            linkL.stringValue = url.map { "🔗 \($0)" } ?? "🔗 ссылка на созвон не указана"
            linkBtn.isEnabled = (url != nil)
            prepBtn.isEnabled = true
            statusL.stringValue = (alreadyPrepped(p) ? "✅ Подготовлено.  " : "⏳ Ожидает подготовки.  ") + lastLogLine()
        } else {
            nameL.stringValue = "Нет предстоящих собесов"
            metaL.stringValue = ""; resumeL.stringValue = ""; linkL.stringValue = ""
            linkBtn.isEnabled = false; prepBtn.isEnabled = false
            statusL.stringValue = lastLogLine()
        }
    }

    @objc func doPrep() { if let p = plan { prepare(p, manual: true); refresh() } }
    @objc func openSchedule() { spawn("/usr/bin/open", [plan?.cockpitUrl ?? "https://jobs.systeam.kz/cabinet"]) }
    @objc func openLink() { if let u = plan?.joinUrl ?? plan?.bookingUrl { spawn("/usr/bin/open", [u]) } }

    // Re-open / dock-click / second launch → bring the window to the front (not a new window).
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        return true
    }
    // Closing the window keeps the app (+ its auto-prep timer) alive.
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
}

// ---- single-instance guard: a second launch re-focuses the already-running window, then exits ----
let myID = Bundle.main.bundleIdentifier ?? "com.jobfinder.cockpit.native"
let others = NSRunningApplication.runningApplications(withBundleIdentifier: myID)
    .filter { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
if let running = others.first {
    running.activate(options: [.activateAllWindows])
    exit(0)
}

let app = NSApplication.shared
let delegate = Cockpit()
app.delegate = delegate
app.setActivationPolicy(.regular)   // normal windowed app with a Dock icon
app.run()
