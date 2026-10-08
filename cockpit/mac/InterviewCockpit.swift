// Interview Cockpit — native macOS menu-bar app for interviewer Alan Bai.
//
// A real .app (NOT a browser-URL shortcut): it reads the server-staged manifest
// ~/NativelyInbox/next.json (written by backend.interviews.cockpit_prep), shows the next собес /
// hiring event in the menu bar, and ~20 min before it (or on «Подготовить сейчас») makes the laptop
// sit-down-and-go:
//   (a) keeps the Mac awake (caffeinate -dimsu),
//   (b) opens Natively (its cockpit résumé-watcher auto-loads THAT candidate's résumé),
//   (c) opens the default browser at the candidate's Zoom/Teams/Meet room (join_url), else the
//       recruiter booking link, else the cockpit schedule.
// Menu-bar only (LSUIElement/.accessory). Never kills Natively, never touches OBS/camera/CDP.
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

struct Plan {
    var mailbox = ""
    var name = ""
    var company = ""
    var startRaw = ""
    var startTs: Date?
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

func targetURL(_ p: Plan) -> String {
    if let j = p.joinUrl { return j }
    if let b = p.bookingUrl { return b }
    return p.cockpitUrl
}

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
    var statusItem: NSStatusItem!
    var plan: Plan?

    func applicationDidFinishLaunching(_ note: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🎧 Собес"
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.tick() }
        tick()
        logLine("Interview Cockpit started")
    }

    func tick() {
        plan = readPlan()
        rebuildMenu()
        guard let p = plan, let st = p.startTs else { return }
        let delta = st.timeIntervalSinceNow
        // within the 20-min lead window (and not already past by >1h) → auto-prepare once
        if delta <= PREP_LEAD && delta > -3600 && !alreadyPrepped(p) {
            prepare(p, manual: false)
            rebuildMenu()
        }
    }

    func rebuildMenu() {
        let menu = NSMenu()
        if let p = plan {
            add(menu, "👤 \(p.name)")
            add(menu, "🏢 \(p.company.isEmpty ? "—" : p.company)  ·  🕒 \(fmtWhen(p.startTs))")
            add(menu, alreadyPrepped(p) ? "✅ Готово к собесу" : "⏳ Ожидает подготовки")
            menu.addItem(.separator())
            add(menu, "Подготовить сейчас", #selector(doPrep), "p")
            add(menu, "Открыть расписание", #selector(openSchedule), "s")
        } else {
            add(menu, "Нет предстоящих собесов")
        }
        menu.addItem(.separator())
        add(menu, "Выход", #selector(quit), "q")
        statusItem.menu = menu
        if let p = plan { statusItem.button?.title = alreadyPrepped(p) ? "🎧 ✅" : "🎧 Собес" }
    }

    @discardableResult
    func add(_ menu: NSMenu, _ title: String, _ action: Selector? = nil, _ key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        if action != nil { item.target = self }
        menu.addItem(item)
        return item
    }

    @objc func doPrep() { if let p = plan { prepare(p, manual: true); rebuildMenu() } }
    @objc func openSchedule() { if let p = plan { spawn("/usr/bin/open", [p.cockpitUrl]) } }
    @objc func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = Cockpit()
app.delegate = delegate
app.setActivationPolicy(.accessory)   // menu-bar only, no Dock icon
app.run()
