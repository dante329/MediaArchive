// media-archive 菜单栏 App
// 菜单栏图标 + 全局快捷键 ⌥⌘D + 可选剪贴板自动监听
// 构建：swiftc -O main.swift -o MediaArchive

import Cocoa
import Carbon
import WebKit
import SwiftUI

let ENGINE = NSHomeDirectory() + "/bin/media-archive/engine.py"
let PYTHON = "/usr/bin/python3"
let ARCHIVE = NSHomeDirectory() + "/Media/Archive"
let DEFAULTS_KEY = "autoWatchEnabled"


// MARK: - 配置
struct Config {
    static var url: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".media-archive")
            .appendingPathComponent("config.json")
    }
    static func load() -> [String: Any] {
        guard let d = try? Data(contentsOf: url),
              let o = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] else { return [:] }
        return o
    }
    static func save(_ o: [String: Any]) {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        if let d = try? JSONSerialization.data(withJSONObject: o,
                                               options: [.prettyPrinted, .sortedKeys]) {
            try? d.write(to: url)
        }
    }
}

// MARK: - 设置模型
final class SettingsModel: ObservableObject {
    @Published var archiveDir: String = ""
    @Published var hotkeyText: String = "⌥⌘D"
    @Published var autoWatch: Bool = false
    @Published var recording: Bool = false
    @Published var webStatus: String = "正在检查…"
    var hotkey: [String: Any] = [:]
}

// MARK: - 设置界面
struct SettingsView: View {
    @ObservedObject var model: SettingsModel
    var onPick: () -> Void
    var onOpenDir: () -> Void
    var onRecord: () -> Void
    var onOpenWeb: () -> Void
    var onRestartWeb: () -> Void
    var onSave: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("媒体归档 · 设置").font(.title2).bold()

            VStack(alignment: .leading, spacing: 8) {
                Text("存放位置").font(.headline)
                HStack(spacing: 8) {
                    Text(model.archiveDir)
                        .font(.system(.body, design: .monospaced))
                        .lineLimit(1).truncationMode(.middle)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Button("选择…", action: onPick)
                    Button("打开", action: onOpenDir)
                }
                Text("改完后已归档的文件不会自动搬走，只影响之后的新内容。")
                    .font(.caption).foregroundColor(.secondary)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("全局快捷键").font(.headline)
                HStack(spacing: 10) {
                    Text(model.recording ? "请按下组合键…" : model.hotkeyText)
                        .font(.system(size: 15, weight: .medium, design: .monospaced))
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 8)
                            .fill(model.recording ? Color.accentColor.opacity(0.25)
                                                  : Color.secondary.opacity(0.15)))
                    Button(model.recording ? "取消录制" : "录制新快捷键", action: onRecord)
                }
                Text("至少包含一个修饰键（⌘ ⌥ ⌃ ⇧），例如 ⌥⌘D。")
                    .font(.caption).foregroundColor(.secondary)
            }

            Divider()

            Toggle(isOn: $model.autoWatch) {
                Text("自动监听剪贴板")
                Text("复制抖音/小红书等白名单链接后自动归档").font(.caption).foregroundColor(.secondary)
            }

            Divider()

            VStack(alignment: .leading, spacing: 8) {
                Text("Web 管理页").font(.headline)
                HStack {
                    Text(model.webStatus).foregroundColor(.secondary)
                    Spacer()
                    Button("在浏览器打开", action: onOpenWeb)
                    Button("重启服务", action: onRestartWeb)
                }
                Text("地址 http://127.0.0.1:8787，也可以直接从本 App 窗口访问。")
                    .font(.caption).foregroundColor(.secondary)
            }

            Divider()
            HStack {
                Spacer()
                Button("保存并关闭", action: onSave).keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 560)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var autoItem: NSMenuItem!
    var recentMenu: NSMenu!
    var watchTimer: Timer?
    var lastChangeCount = NSPasteboard.general.changeCount
    var lastHandled = ""
    var running = false
    var hotKeyRef: EventHotKeyRef?
    var mainWindow: NSWindow?
    var settingsWindow: NSWindow?
    var settingsModel = SettingsModel()
    var recordingMonitor: Any?
    var pendingHotkey: [String: Any] = [:]
    var hotKeyKeyCode: UInt32 = 2
    var hotKeyMods: UInt32 = UInt32(cmdKey | optionKey)
    var hotkeyDisplay = "⌥⌘D"

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.length = NSStatusItem.squareLength
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.black.setStroke()
            NSColor.black.setFill()
            let shaft = NSBezierPath()
            shaft.move(to: NSPoint(x: 9, y: 15))
            shaft.line(to: NSPoint(x: 9, y: 6))
            shaft.lineWidth = 2
            shaft.stroke()
            let head = NSBezierPath()
            head.move(to: NSPoint(x: 4.5, y: 8))
            head.line(to: NSPoint(x: 9, y: 3.5))
            head.line(to: NSPoint(x: 13.5, y: 8))
            head.lineWidth = 2
            head.stroke()
            NSBezierPath(rect: NSRect(x: 4, y: 0.5, width: 10, height: 2)).fill()
            return true
        }
        img.isTemplate = true
        statusItem.button?.image = img
        statusItem.button?.toolTip = "媒体归档"
        let diag = "item=\(statusItem != nil) btn=\(statusItem.button != nil) title=\(statusItem.button?.title ?? "nil") visible=\(statusItem.isVisible) len=\(statusItem.length)\n"
        try? diag.write(toFile: "/tmp/ma-diag.txt", atomically: true, encoding: .utf8)
        buildMenu()
        applyStoredHotkey()
        if UserDefaults.standard.bool(forKey: DEFAULTS_KEY) {
            setAutoWatch(true)
        }
        // 每次启动都直接给窗口：这台机器上菜单栏图标不可靠，窗口是唯一入口
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.openWindow() }
    }

    // MARK: 菜单
    func buildMenu() {
        let menu = NSMenu()

        let winItem = NSMenuItem(title: "打开主窗口", action: #selector(openWindow), keyEquivalent: "o")
        menu.addItem(winItem)
        let setItem = NSMenuItem(title: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        menu.addItem(setItem)
        menu.addItem(NSMenuItem.separator())

        let clipItem = NSMenuItem(title: "归档剪贴板链接", action: #selector(archiveClipboard), keyEquivalent: "d")
        clipItem.keyEquivalentModifierMask = [.command, .option]
        menu.addItem(clipItem)

        let browserItem = NSMenuItem(title: "归档当前浏览器标签页", action: #selector(archiveBrowserTab), keyEquivalent: "e")
        browserItem.keyEquivalentModifierMask = [.command, .option]
        menu.addItem(browserItem)

        let clipRawItem = NSMenuItem(title: "归档剪贴板（不改写剪贴板）", action: #selector(archiveClipboardKeep), keyEquivalent: "")
        menu.addItem(clipRawItem)
        menu.addItem(NSMenuItem.separator())

        autoItem = NSMenuItem(title: "自动监听剪贴板", action: #selector(toggleAuto), keyEquivalent: "")
        autoItem.state = UserDefaults.standard.bool(forKey: DEFAULTS_KEY) ? .on : .off
        menu.addItem(autoItem)
        menu.addItem(NSMenuItem.separator())

        let recentRoot = NSMenuItem(title: "最近归档", action: nil, keyEquivalent: "")
        recentMenu = NSMenu()
        recentRoot.submenu = recentMenu
        menu.addItem(recentRoot)

        menu.addItem(NSMenuItem(title: "刷新最近记录", action: #selector(refreshRecent), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "打开归档目录", action: #selector(openArchive), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "打开索引表", action: #selector(openIndex), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "打开 Web 管理页", action: #selector(openWeb), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q"))

        statusItem.menu = menu
        refreshRecent()
    }

    @objc func refreshRecent() {
        recentMenu.removeAllItems()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: PYTHON)
        p.arguments = [ENGINE, "--list", "8"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        try? p.run()
        p.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let text = String(data: data, encoding: .utf8) ?? ""
        let lines = text.split(separator: "\n").map(String.init).filter { !$0.isEmpty }
        if lines.isEmpty {
            recentMenu.addItem(NSMenuItem(title: "（暂无记录）", action: nil, keyEquivalent: ""))
            return
        }
        for line in lines {
            let it = NSMenuItem(title: line, action: #selector(noop), keyEquivalent: "")
            it.isEnabled = false
            recentMenu.addItem(it)
        }
    }

    // MARK: 动作
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { openWindow() }
        return true
    }

    @objc func noop() {}

    
// MARK: - 主窗口界面
struct WebPanel: NSViewRepresentable {
    let url: URL
    func makeNSView(context: Context) -> WKWebView {
        let w = WKWebView()
        w.load(URLRequest(url: url))
        return w
    }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

struct MainView: View {
    var onArchiveClip: () -> Void
    var onOpenSettings: () -> Void
    var onOpenDir: () -> Void
    var onOpenWeb: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Button("归档剪贴板", action: onArchiveClip)
                Button("设置…", action: onOpenSettings)
                Button("打开归档目录", action: onOpenDir)
                Button("在浏览器打开", action: onOpenWeb)
                Spacer()
                Text("把链接粘到下方输入框，回车即可归档")
                    .font(.caption).foregroundColor(.secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            Divider()
            WebPanel(url: URL(string: "http://127.0.0.1:8787")!)
        }
    }
}

// MARK: - 设置窗口
    @objc func openSettings() {
        if let w = settingsWindow {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        settingsModel = SettingsModel()
        settingsModel.archiveDir = currentArchiveDir()
        settingsModel.hotkeyText = hotkeyDisplay
        settingsModel.autoWatch = UserDefaults.standard.bool(forKey: DEFAULTS_KEY)
        settingsModel.webStatus = webAlive() ? "运行中 · http://127.0.0.1:8787" : "未运行"
        pendingHotkey = [:]

        let view = SettingsView(model: settingsModel,
            onPick: { [weak self] in self?.pickArchiveDir() },
            onOpenDir: { [weak self] in self?.openArchive() },
            onRecord: { [weak self] in self?.toggleRecording() },
            onOpenWeb: { [weak self] in self?.openWeb() },
            onRestartWeb: { [weak self] in self?.restartWeb() },
            onSave: { [weak self] in self?.saveSettings() })

        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 620),
                         styleMask: [.titled, .closable],
                         backing: .buffered, defer: false)
        w.title = "媒体归档 · 设置"
        w.contentView = NSHostingView(rootView: view)
        w.center()
        w.isReleasedWhenClosed = false
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow = w
    }

    func currentArchiveDir() -> String {
        let cfg = Config.load()
        if let d = cfg["archive_dir"] as? String, !d.isEmpty { return d }
        return NSHomeDirectory() + "/Media/Archive"
    }

    func pickArchiveDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "选用此文件夹"
        panel.message = "选择视频与图文的存放位置"
        panel.directoryURL = URL(fileURLWithPath: currentArchiveDir())
        if panel.runModal() == .OK, let u = panel.url {
            settingsModel.archiveDir = u.path
        }
    }

    func toggleRecording() {
        if settingsModel.recording { stopRecording(); return }
        settingsModel.recording = true
        settingsModel.hotkeyText = "请按下组合键…"
        recordingMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] ev in
            guard let self = self, self.settingsModel.recording else { return ev }
            let mods = ev.modifierFlags.intersection([.command, .option, .control, .shift])
            if mods.isEmpty { NSSound.beep(); return nil }
            var names: [String] = []
            if mods.contains(.control) { names.append("control") }
            if mods.contains(.option) { names.append("option") }
            if mods.contains(.shift) { names.append("shift") }
            if mods.contains(.command) { names.append("command") }
            let key = (ev.charactersIgnoringModifiers ?? "").uppercased()
            let disp = self.displayFor(names: names, key: key)
            self.pendingHotkey = ["keyCode": Int(ev.keyCode), "modifiers": names, "display": disp]
            self.settingsModel.hotkeyText = disp
            self.stopRecording()
            return nil
        }
    }

    func stopRecording() {
        settingsModel.recording = false
        if let m = recordingMonitor {
            NSEvent.removeMonitor(m)
            recordingMonitor = nil
        }
        if pendingHotkey["display"] as? String == nil {
            settingsModel.hotkeyText = hotkeyDisplay
        }
    }

    func saveSettings() {
        var cfg = Config.load()
        cfg["archive_dir"] = settingsModel.archiveDir
        if !pendingHotkey.isEmpty { cfg["hotkey"] = pendingHotkey }
        cfg["auto_watch"] = settingsModel.autoWatch
        Config.save(cfg)
        UserDefaults.standard.set(settingsModel.autoWatch, forKey: DEFAULTS_KEY)
        setAutoWatch(settingsModel.autoWatch)
        applyStoredHotkey()
        restartWeb()
        settingsWindow?.close()
        settingsWindow = nil
        notify("设置已保存", "存放位置与快捷键 " + hotkeyDisplay + " 已生效")
    }

    func webAlive() -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
        p.arguments = ["-s", "-o", "/dev/null", "-m", "2", "http://127.0.0.1:8787/api/stats"]
        try? p.run()
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    func restartWeb() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "-k", "gui/\(getuid())/com.dante.mediaarchive-web"]
        try? p.run()
        p.waitUntilExit()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
            guard let self = self, self.settingsWindow != nil else { return }
            self.settingsModel.webStatus = self.webAlive()
                ? "运行中 · http://127.0.0.1:8787" : "未运行"
        }
    }

    @objc func openWindow() {
        if let w = mainWindow {
            NSApp.setActivationPolicy(.regular)
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 760),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "媒体归档"
        w.center()
        w.isReleasedWhenClosed = false
        let view = MainView(
            onArchiveClip: { [weak self] in self?.archiveClipboard() },
            onOpenSettings: { [weak self] in self?.openSettings() },
            onOpenDir: { [weak self] in self?.openArchive() },
            onOpenWeb: { [weak self] in self?.openWeb() })
        w.contentView = NSHostingView(rootView: view)
        mainWindow = w
        NSApp.setActivationPolicy(.regular)
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func archiveClipboard() { archive(clipboardText()) }

    @objc func archiveBrowserTab() {
        if let u = browserURL() {
            archive(u)
        } else {
            alert("读不到浏览器地址", "请让 Chrome / Safari 在前台，或直接复制链接后按 ⌥⌘D。")
        }
    }

    func browserURL() -> String? {
        let script = """
        tell application "Google Chrome"
            if (count of windows) > 0 then
                return URL of active tab of front window
            end if
        end tell
        """
        var err: NSDictionary?
        if let sc = NSAppleScript(source: script) {
            let r = sc.executeAndReturnError(&err)
            if err == nil, let v = r.stringValue, !v.isEmpty { return v }
        }
        let safari = """
        tell application "Safari"
            if (count of windows) > 0 then
                return URL of current tab of front window
            end if
        end tell
        """
        var err2: NSDictionary?
        if let sc = NSAppleScript(source: safari) {
            let r = sc.executeAndReturnError(&err2)
            if err2 == nil, let v = r.stringValue, !v.isEmpty { return v }
        }
        return nil
    }
    @objc func archiveClipboardKeep() { archive(clipboardText()) }

    func clipboardText() -> String {
        return NSPasteboard.general.string(forType: .string) ?? ""
    }

    func archive(_ text: String) {
        guard !running else {
            alert("正在归档中", "上一条还没处理完，稍等。")
            return
        }
        let url = extractURL(text)
        guard let u = url else {
            alert("没找到链接", "剪贴板里没有可识别的链接。")
            return
        }
        running = true
        statusItem.button?.toolTip = "正在归档…"
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: PYTHON)
            p.arguments = [ENGINE, u]
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
            p.environment = env
            var output = ""
            do {
                try p.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                output = String(data: data, encoding: .utf8) ?? ""
                p.waitUntilExit()
            } catch {
                output = "启动失败：\(error)"
            }
            DispatchQueue.main.async {
                self.running = false
                self.statusItem.button?.toolTip = "媒体归档"
                self.refreshRecent()
                if output.contains("归档：") {
                    let folder = output.split(separator: "\n")
                        .last(where: { $0.contains("归档：") })?
                        .replacingOccurrences(of: "[", with: "")
                        .replacingOccurrences(of: "]", with: "") ?? ""
                    self.notify("已保存", String(folder.suffix(90)))
                } else {
                    self.notify("归档失败", output.suffix(120).description)
                }
            }
        }
    }

    func extractURL(_ text: String) -> String? {
        let pattern = "https?://[^\\s\"'<>，。、）)】]+"
        guard let re = try? NSRegularExpression(pattern: pattern) else { return nil }
        let ns = text as NSString
        guard let m = re.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)) else { return nil }
        return ns.substring(with: m.range)
    }

    @objc func toggleAuto() {
        setAutoWatch(!UserDefaults.standard.bool(forKey: DEFAULTS_KEY))
    }

    func setAutoWatch(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: DEFAULTS_KEY)
        autoItem.state = on ? .on : .off
        watchTimer?.invalidate()
        watchTimer = nil
        if on {
            lastChangeCount = NSPasteboard.general.changeCount
            watchTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
                self.pollClipboard()
            }
        }
    }

    func pollClipboard() {
        let pb = NSPasteboard.general
        guard pb.changeCount != lastChangeCount else { return }
        lastChangeCount = pb.changeCount
        let text = pb.string(forType: .string) ?? ""
        guard let url = extractURL(text), url != lastHandled else { return }
        guard shouldHandle(url) else { return }
        lastHandled = url
        archive(url)
    }

    func shouldHandle(_ url: String) -> Bool {
        let hosts = ["v.douyin.com", "douyin.com", "xhslink.com", "xiaohongshu.com",
                     "x.com", "twitter.com", "bilibili.com", "b23.tv", "youtube.com", "youtu.be",
                     "instagram.com", "weibo.com", "reddit.com", "t.me", "tiktok.com"]
        guard let host = URL(string: url)?.host?.lowercased() else { return false }
        return hosts.contains { host == $0 || host.hasSuffix("." + $0) }
    }

    @objc func openArchive() { NSWorkspace.shared.open(URL(fileURLWithPath: currentArchiveDir())) }
    @objc func openIndex() {
        let csv = ARCHIVE + "/索引.csv"
        if FileManager.default.fileExists(atPath: csv) {
            NSWorkspace.shared.open(URL(fileURLWithPath: csv))
        } else {
            alert("还没有索引", "先归档几条内容。")
        }
    }
    @objc func openWeb() { NSWorkspace.shared.open(URL(string: "http://127.0.0.1:8787")!) }
    @objc func quit() { NSApp.terminate(nil) }

    func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.alertStyle = .informational
        a.addButton(withTitle: "好")
        a.runModal()
    }

    func notify(_ title: String, _ text: String) {
        // 用 osascript 发通知，避免申请通知权限失败时静默
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        let safe = text.replacingOccurrences(of: "\"", with: "'")
        let safeTitle = title.replacingOccurrences(of: "\"", with: "'")
        p.arguments = ["-e", "display notification \"\(safe)\" with title \"\(safeTitle)\""]
        try? p.run()
    }

    // MARK: 全局快捷键
    func applyStoredHotkey() {
        let cfg = Config.load()
        if let hk = cfg["hotkey"] as? [String: Any],
           let kc = hk["keyCode"] as? Int,
           let mods = hk["modifiers"] as? [String] {
            hotKeyKeyCode = UInt32(kc)
            hotKeyMods = carbonMods(mods)
            hotkeyDisplay = (hk["display"] as? String) ?? "⌥⌘D"
        }
        registerHotKey()
    }

    func carbonMods(_ names: [String]) -> UInt32 {
        var m: UInt32 = 0
        if names.contains("command") { m |= UInt32(cmdKey) }
        if names.contains("option") { m |= UInt32(optionKey) }
        if names.contains("control") { m |= UInt32(controlKey) }
        if names.contains("shift") { m |= UInt32(shiftKey) }
        return m
    }

    func displayFor(names: [String], key: String) -> String {
        var out = ""
        if names.contains("control") { out += "⌃" }
        if names.contains("option") { out += "⌥" }
        if names.contains("shift") { out += "⇧" }
        if names.contains("command") { out += "⌘" }
        return out + key
    }

    func registerHotKey() {
        if let ref = hotKeyRef {
            UnregisterEventHotKey(ref)
            hotKeyRef = nil
        }
        let hotKeyID = EventHotKeyID(signature: OSType(0x4D415243), id: 1) // 'MARC'
        let status = RegisterEventHotKey(hotKeyKeyCode,
                                        hotKeyMods,
                                        hotKeyID,
                                        GetApplicationEventTarget(),
                                        0,
                                        &hotKeyRef)
        if status != noErr {
            NSLog("注册快捷键失败：\(status)")
        }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { (_, event, _) -> OSStatus in
            DispatchQueue.main.async {
                (NSApp.delegate as? AppDelegate)?.archiveClipboard()
            }
            return noErr
        }, 1, &spec, nil, nil)
        _ = hotKeyID
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
