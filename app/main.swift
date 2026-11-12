// media-archive 菜单栏 App
// 菜单栏图标 + 全局快捷键 ⌥⌘D + 可选剪贴板自动监听
// 构建：swiftc -O main.swift -o MediaArchive

import Cocoa

/// 主窗口的 contentView 是 NSHostingView（SwiftUI 包装），
/// 想拿到里面的 WKWebView 只能自己存引用
final class WebHolder {
    static var main: WKWebView?
}

import Carbon
import WebKit
import SwiftUI
import Photos
import UserNotifications

func isUnderArchive(_ path: String?) -> Bool {
    guard let raw = path?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else { return false }
    let root = ((Config.load()["archive_dir"] as? String) ?? (NSHomeDirectory() + "/Media/Archive"))
    let rp = URL(fileURLWithPath: raw).resolvingSymlinksInPath().standardizedFileURL.path
    let rr = URL(fileURLWithPath: root).resolvingSymlinksInPath().standardizedFileURL.path
    // Reject paths that resolve to temp directories even if symlinks point into archive
    if rp.hasPrefix("/tmp") || rp.hasPrefix("/var/folders") { return false }
    return rp == rr || rp.hasPrefix(rr + "/")
}

func isWebURL(_ u: URL) -> Bool {
    guard let s = u.scheme?.lowercased() else { return false }
    return s == "http" || s == "https"
}

/// 只允许打开已知平台的客户端深链，避免被任意 scheme 唤起
func isNativeSchemeAllowed(_ u: URL) -> Bool {
    let s = (u.scheme ?? "").lowercased()
    return ["xhsdiscover", "xhsopen", "douyin-pc", "snssdk1128", "tg", "bilibili", "youtube",
            "instagram", "reddit", "sinaweibo", "snssdk1233", "kwai"].contains(s)
}

func enginePath() -> String {
    if let d = Config.load()["install_dir"] as? String, !d.isEmpty {
        return d + "/engine.py"
    }
    return NSHomeDirectory() + "/bin/media-archive/engine.py"
}
let ENGINE = enginePath()
let PYTHON = "/usr/bin/python3"
let PY_ML = "/opt/homebrew/bin/python3.10"
let LIVEPHOTO_SCRIPT = (ENGINE as NSString).deletingLastPathComponent + "/livephoto.py"
let ARCHIVE = NSHomeDirectory() + "/Media/Archive"
let DEFAULTS_KEY = "autoWatchEnabled"

/// 支持的平台与域名（自动监听用）
let PLATFORMS: [(id: String, name: String, hosts: [String])] = [
    ("douyin",      "抖音",         ["v.douyin.com", "douyin.com", "iesdouyin.com"]),
    ("xiaohongshu", "小红书",       ["xhslink.com", "xhslink.cn", "xiaohongshu.com"]),
    ("bilibili",    "B站",          ["bilibili.com", "b23.tv", "bili2233.cn"]),
    ("youtube",     "YouTube",      ["youtube.com", "youtu.be"]),
    ("x",           "X / Twitter",  ["x.com", "twitter.com", "t.co"]),
    ("instagram",   "Instagram",    ["instagram.com", "instagr.am"]),
    ("weibo",       "微博",         ["weibo.com", "weibo.cn", "t.cn"]),
    ("reddit",      "Reddit",       ["reddit.com", "redd.it"]),
    ("telegram",    "Telegram",     ["t.me", "telegram.me"]),
    ("tiktok",      "TikTok",       ["tiktok.com", "vt.tiktok.com"]),
    ("kuaishou",    "快手",         ["kuaishou.com", "v.kuaishou.com"]),
]
let ALL_PLATFORM_IDS = PLATFORMS.map { $0.id }


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
    @discardableResult
    static func save(_ o: [String: Any]) -> Bool {
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            let d = try JSONSerialization.data(withJSONObject: o,
                                               options: [.prettyPrinted, .sortedKeys])
            try d.write(to: url)
            return true
        } catch {
            NSLog("Config save failed: %@", error.localizedDescription)
            return false
        }
    }
}

// MARK: - 设置模型
final class SettingsModel: ObservableObject {
    @Published var archiveDir: String = ""
    @Published var hotkeyText: String = "⌥⌘D"
    @Published var autoWatch: Bool = false
    @Published var notifyStart: Bool = true
    @Published var notifyDone: Bool = true
    @Published var theme: String = "auto"
    @Published var notifyExists: Bool = true
    @Published var notifyError: Bool = true
    @Published var notifyAudio: Bool = true
    @Published var notifyLive: Bool = true
    @Published var notifyRefresh: Bool = false
    @Published var notifySettings: Bool = false
    @Published var ytdlpBrowserCookies: Bool = false
    /// 上次保存时的快照，用来判断"有没有改动"
    @Published private var savedSnapshot: String = ""

    var snapshot: String {
        [
            archiveDir, hotkeyText, String(autoWatch),
            watchPlatforms.sorted().joined(separator: ","),
            theme,
            String(notifyStart), String(notifyDone), String(notifyExists),
            String(notifyError), String(notifyAudio), String(notifyLive),
            String(notifyRefresh), String(notifySettings),
            String(ytdlpBrowserCookies),
            livephotoMode
        ].joined(separator: "|")
    }

    /// 还没建立快照（刚打开窗口）时视为无改动，避免按钮先亮后暗的闪烁
    var hasChanges: Bool {
        guard !savedSnapshot.isEmpty else { return false }
        return snapshot != savedSnapshot
    }

    func markSaved() { savedSnapshot = snapshot }
    @Published var watchPlatforms: [String] = ALL_PLATFORM_IDS
    @Published var recording: Bool = false
    @Published var webStatus: String = "正在检查…"
    @Published var livephotoMode: String = "both"
    @Published var recordingTarget: String = ""
    @Published var importAllKey: String = "⇧⌘I"
    @Published var extractKey: String = "⌘E"
    @Published var importFolderKey: String = "⌘I"
    var hotkey: [String: Any] = [:]
}

// MARK: - 设置界面
/// 从左到右排列，放不下自动换行（每项靠左对齐）
struct FlowLayout: Layout {
    var spacing: CGFloat = 10
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxW = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > maxW && x > 0 {
                x = 0
                y += rowH + lineSpacing
                rowH = 0
            }
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
        return CGSize(width: maxW == .infinity ? x : maxW, height: y + rowH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowH: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x + s.width > bounds.maxX && x > bounds.minX {
                x = bounds.minX
                y += rowH + lineSpacing
                rowH = 0
            }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: SettingsModel
    var onPick: () -> Void
    var onOpenDir: () -> Void
    var onRecord: () -> Void
    var onOpenWeb: () -> Void
    var onRestartWeb: () -> Void
    var onRecordMenu: (String) -> Void
    var onSave: () -> Void
    var onClose: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("设置").font(.title2).bold()

                group("存放位置") {
                    HStack(spacing: 8) {
                        Text(model.archiveDir)
                            .font(.system(.caption, design: .monospaced))
                            .lineLimit(1).truncationMode(.middle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Button("选择…", action: onPick)
                        Button("打开", action: onOpenDir)
                    }
                }

                group("快捷键") {
                    keyRow("归档剪贴板（全局）", model.hotkeyText, target: "global",
                           display: model.recordingTarget == "global" ? "按下组合键…" : nil,
                           action: onRecord)
                    keyRow("导入所有实况", model.importAllKey, target: "importAll",
                           display: model.recordingTarget == "importAll" ? "按下组合键…" : nil,
                           action: { onRecordMenu("importAll") })
                    keyRow("窗口内提取实况", model.importFolderKey, target: "importFolder",
                           display: model.recordingTarget == "importFolder" ? "按下组合键…" : nil,
                           action: { onRecordMenu("importFolder") })
                    keyRow("提取当前音频", model.extractKey, target: "extractAudio",
                           display: model.recordingTarget == "extractAudio" ? "按下组合键…" : nil,
                           action: { onRecordMenu("extractAudio") })
                }



                group("通知") {
                    // 固定宽度 + 左对齐，排满自动换行，复选框成列对齐
                    FlowLayout(spacing: 4, lineSpacing: 6) {
                        notifyToggle("归档开始", $model.notifyStart)
                        notifyToggle("归档成功", $model.notifyDone)
                        notifyToggle("已存在", $model.notifyExists)
                        notifyToggle("归档失败", $model.notifyError)
                        notifyToggle("音频提取完成", $model.notifyAudio)
                        notifyToggle("实况导入完成", $model.notifyLive)
                        notifyToggle("刷新页面", $model.notifyRefresh)
                        notifyToggle("设置保存", $model.notifySettings)
                    }
                }

                group("自动监听") {
                    Toggle("复制白名单链接后自动归档", isOn: $model.autoWatch)
                    if model.autoWatch {
                        Text("监听的平台").font(.caption).foregroundColor(.secondary)
                            .padding(.top, 2)
                        FlowLayout(spacing: 14, lineSpacing: 7) {
                            ForEach(PLATFORMS, id: \.id) { pf in
                                Toggle(pf.name, isOn: Binding(
                                    get: { model.watchPlatforms.contains(pf.id) },
                                    set: { on in
                                        if on {
                                            if !model.watchPlatforms.contains(pf.id) {
                                                model.watchPlatforms.append(pf.id)
                                            }
                                        } else {
                                            model.watchPlatforms.removeAll { $0 == pf.id }
                                        }
                                    }))
                                    .toggleStyle(.checkbox)
                                    .font(.system(size: 12))
                                    .fixedSize()
                            }
                        }
                        HStack(spacing: 8) {
                            Button("全选") { model.watchPlatforms = ALL_PLATFORM_IDS }
                                .controlSize(.small)
                            Button("全不选") { model.watchPlatforms = [] }
                                .controlSize(.small)
                            Spacer()
                        }
                    }
                }

                group("下载引擎") {
                    Toggle("允许 yt-dlp 使用本机 Chrome 登录态", isOn: $model.ytdlpBrowserCookies)
                    Text("开启后，yt-dlp 会读取你日常 Chrome 的登录状态（--cookies-from-browser chrome），用于下载需要登录的第三方站点；默认关闭。")
                        .font(.caption).foregroundColor(.secondary)
                }

                group("实况图保存方式") {
                    Picker("", selection: $model.livephotoMode) {
                        Text("图片 + 视频").tag("both")
                        Text("只保存图片").tag("image")
                        Text("只保存视频").tag("video")
                    }
                    .pickerStyle(.radioGroup).labelsHidden()
                }

                group("Web 管理页") {
                    HStack {
                        Text(model.webStatus).font(.caption).foregroundColor(.secondary)
                        Spacer()
                        Button("打开", action: onOpenWeb)
                        Button("重启", action: onRestartWeb)
                    }
                }

                group("外观") {
                    Picker("", selection: $model.theme) {
                        Text("默认").tag("default")
                        Text("深夜").tag("deep")
                        Text("暮光").tag("twilight")
                        Text("薄荷").tag("mint")
                        Text("拿铁").tag("latte")
                        Text("靛蓝").tag("indigo")
                        Text("珊瑚").tag("coral")
                        Text("冰川").tag("glacier")
                        Text("极光").tag("aurora")
                        Text("石墨").tag("graphite")
                        Text("跟随系统").tag("auto")
                    }
                    .pickerStyle(.radioGroup).labelsHidden()
                }

                HStack {
                    Button("关闭", action: onClose)
                    Spacer()
                    Button("保存", action: onSave)
                        .keyboardShortcut(.defaultAction)
                        .disabled(!model.hasChanges)
                }
                .padding(.top, 4)
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollContentBackground(.hidden)
        .background(Color.clear)
        .frame(width: 520, height: 640)
    }

    @ViewBuilder
    private func notifyToggle(_ label: String, _ binding: Binding<Bool>) -> some View {
        Toggle(label, isOn: binding)
            .toggleStyle(.checkbox)
            .font(.system(size: 12))
            .frame(width: 132, alignment: .leading)
    }

    @ViewBuilder
    private func keyRow(_ label: String, _ value: String, target: String,
                        display: String?, action: @escaping () -> Void) -> some View {
        let isRecording = display != nil
        HStack(spacing: 10) {
            Text(label)
                .frame(width: 92, alignment: .leading)
                .font(.caption).foregroundColor(.secondary)
            Text(display ?? value)
                .font(.system(size: 13, weight: .medium, design: .monospaced))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(width: 58)
                .padding(.vertical, 4)
                .background(RoundedRectangle(cornerRadius: 6)
                    .fill(isRecording ? Color.accentColor.opacity(0.22)
                                      : Color.secondary.opacity(0.12)))
            Spacer()
            Button(isRecording ? "取消" : "录制", action: action)
                .controlSize(.small)
        }
    }

    @ViewBuilder
    private func group<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.headline)
            content()
        }
    }

}

final class LiveImport: NSObject {
    static let shared = LiveImport()

    static func alert(_ title: String, _ text: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.alertStyle = .informational
        a.addButton(withTitle: "好")
        a.runModal()
    }

    static func notify(_ title: String, _ text: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = text
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    /// 只导入指定作品目录；路径缺失就报错，绝不擅自扩大范围
    func run(dir: String?) {
        guard let d = dir?.trimmingCharacters(in: .whitespacesAndNewlines), !d.isEmpty else {
            LiveImport.alert("没有收到作品路径",
                             "导入只针对当前打开的这一条作品。\n请从作品窗口右上角点「导入实况照片」。\n\n（菜单 ⌘I 才是导入整个归档）")
            return
        }
        let u = URL(fileURLWithPath: d)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: u.path, isDirectory: &isDir), isDir.boolValue else {
            LiveImport.alert("目录不存在", d)
            return
        }
        prepareThenImport(u)
    }

    /// 菜单用：扫整个归档
    func runAll() {
        let cfg = Config.load()
        let root = (cfg["archive_dir"] as? String) ?? (NSHomeDirectory() + "/Media/Archive")
        prepareThenImport(URL(fileURLWithPath: root))
    }

    /// 只导入指定的配对（预览窗口里勾选的那些）
    func importSelected(_ pairs: [(URL, URL)], done: @escaping (Int, Int, [String]) -> Void) {
        PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
            guard status == .authorized || status == .limited else {
                DispatchQueue.main.async {
                    LiveImport.alert("无法写入相册", "系统设置 → 隐私与安全性 → 照片 → 勾选「媒体归档」")
                }
                return
            }
            var doneCount = 0, ok = 0
            var errors: [String] = []
            let lock = DispatchQueue(label: "import.lock")
            for (img, mov) in pairs {
                PHPhotoLibrary.shared().performChanges({
                    let req = PHAssetCreationRequest.forAsset()
                    let opts = PHAssetResourceCreationOptions()
                    req.addResource(with: .photo, fileURL: img, options: opts)
                    req.addResource(with: .pairedVideo, fileURL: mov, options: opts)
                }) { success, error in
                    lock.sync {
                        doneCount += 1
                        if success { ok += 1 }
                        else if errors.count < 3 {
                            errors.append("\(img.lastPathComponent): \(error?.localizedDescription ?? "未知")")
                        }
                        if doneCount == pairs.count {
                            DispatchQueue.main.async { done(ok, pairs.count, errors) }
                        }
                    }
                }
            }
        }
    }

    /// 导入前自动把 01.jpg + 01_live.mp4 转成合规的 01.mov
    /// 用户不需要知道"重编码"这件事，他要的是"导进相册能当实况用"
    func prepareThenImport(_ dir: URL) {
        let fm = FileManager.default
        var needConvert = false
        if let en = fm.enumerator(at: dir, includingPropertiesForKeys: nil) {
            for case let u as URL in en {
                guard u.pathExtension.lowercased() == "mp4",
                      u.deletingPathExtension().lastPathComponent.hasSuffix("_live") else { continue }
                let img = u.deletingPathExtension().deletingPathExtension()
                    .appendingPathExtension("jpg")
                let mov = img.deletingPathExtension().appendingPathExtension("mov")
                if fm.fileExists(atPath: img.path), !fm.fileExists(atPath: mov.path) {
                    needConvert = true
                    break
                }
            }
        }
        guard needConvert, fm.fileExists(atPath: LIVEPHOTO_SCRIPT) else {
            importPairs(in: dir)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: PY_ML)
            p.arguments = [LIVEPHOTO_SCRIPT, "--dir", dir.path]
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
            p.environment = env
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            try? p.run()
            _ = pipe.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let conversionOK = p.terminationStatus == 0
            DispatchQueue.main.async {
                if !conversionOK {
                    LiveImport.alert("实况照片转换失败", "livephoto.py 返回错误码 \(p.terminationStatus)，跳过导入。")
                    return
                }
                self.importPairs(in: dir)     // 转完再导
            }
        }
    }

    func importPairs(in dir: URL) {
        PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
            guard status == .authorized || status == .limited else {
                DispatchQueue.main.async {
                    let why = status == .denied ? "已被拒绝"
                            : (status == .restricted ? "受系统限制" : "尚未授权")
                    LiveImport.alert("无法写入相册（\(why)）",
                                     "系统设置 → 隐私与安全性 → 照片 → 勾选「媒体归档」，然后重试。")
                }
                return
            }
            let fm = FileManager.default
            var pairs: [(URL, URL)] = []
            if let en = fm.enumerator(at: dir, includingPropertiesForKeys: nil) {
                for case let u as URL in en {
                    let ext = u.pathExtension.lowercased()
                    guard ["jpg", "jpeg", "heic"].contains(ext) else { continue }
                    let mov = u.deletingPathExtension().appendingPathExtension("mov")
                    if fm.fileExists(atPath: mov.path) { pairs.append((u, mov)) }
                }
            }
            if pairs.isEmpty {
                DispatchQueue.main.async {
                    LiveImport.alert("没找到实况配对",
                                     "扫描目录：\(dir.path)\n\n没有「同名 .jpg + .mov」的配对。")
                }
                return
            }
            var done = 0, ok = 0
            var errors: [String] = []
            for (img, mov) in pairs {
                PHPhotoLibrary.shared().performChanges({
                    let req = PHAssetCreationRequest.forAsset()
                    let opts = PHAssetResourceCreationOptions()
                    req.addResource(with: .photo, fileURL: img, options: opts)
                    req.addResource(with: .pairedVideo, fileURL: mov, options: opts)
                }) { success, error in
                    done += 1
                    if success { ok += 1 }
                    else if errors.count < 3 {
                        errors.append("\(img.lastPathComponent): \(error?.localizedDescription ?? "未知错误")")
                    }
                    if done == pairs.count {
                        DispatchQueue.main.async {
                            let detail = errors.isEmpty ? "" : "\n\n失败原因：\n" + errors.joined(separator: "\n")
                            if notifyOn("livephoto") {
                                LiveImport.notify("导入完成", "成功 \(ok)/\(pairs.count) 组")
                            }
                            LiveImport.alert("导入完成", "成功 \(ok) / \(pairs.count) 组。" + detail)
                        }
                    }
                }
            }
        }
    }
}

// MARK: - 从文件夹导入：先预览再导入
struct LivePair: Identifiable {
    let id = UUID()
    let image: URL
    let video: URL
    let thumb: NSImage?
    let ok: Bool
}

final class LivePreviewModel: ObservableObject {
    @Published var folder: URL
    @Published var pairs: [LivePair] = []
    @Published var picked: Set<UUID> = []
    @Published var scanning = true
    @Published var resultText = ""

    init(folder: URL) {
        self.folder = folder
        scan()
    }

    func scan() {
        scanning = true
        let dir = folder
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let fm = FileManager.default
            var found: [LivePair] = []
            if let en = fm.enumerator(at: dir, includingPropertiesForKeys: nil) {
                for case let u as URL in en {
                    let ext = u.pathExtension.lowercased()
                    guard ["jpg", "jpeg", "heic"].contains(ext) else { continue }
                    let mov = u.deletingPathExtension().appendingPathExtension("mov")
                    guard fm.fileExists(atPath: mov.path) else { continue }
                    let img = NSImage(contentsOf: u)
                    found.append(LivePair(image: u, video: mov, thumb: img, ok: img != nil))
                }
            }
            DispatchQueue.main.async {
                self.pairs = found
                self.picked = Set(found.map { $0.id })
                self.scanning = false
            }
        }
    }

    var summary: String {
        if scanning { return "扫描中…" }
        if pairs.isEmpty { return "这个文件夹里没有「同名 .jpg + .mov」的配对" }
        return "找到 \(pairs.count) 组实况配对 · 已选 \(picked.count) 组"
    }
}

struct LivePreviewView: View {
    @ObservedObject var model: LivePreviewModel
    var onImport: () -> Void
    var onClose: () -> Void

    private let cols = [GridItem(.adaptive(minimum: 130), spacing: 10)]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("导入实况照片").font(.title3).bold()
                Spacer()
                Button("关闭", action: onClose)
            }
            Text(model.folder.path)
                .font(.system(.caption, design: .monospaced))
                .lineLimit(1).truncationMode(.middle)
                .foregroundColor(.secondary)

            if model.scanning {
                ProgressView("扫描文件夹…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.pairs.isEmpty {
                VStack(spacing: 8) {
                    Spacer()
                    Image(systemName: "photo.on.rectangle.angled").font(.system(size: 34))
                        .foregroundColor(.secondary)
                    Text("没有找到实况配对").foregroundColor(.secondary)
                    Text("需要「同名 .jpg + .mov」放在一起").font(.caption).foregroundColor(.secondary)
                    Spacer()
                }.frame(maxWidth: .infinity)
            } else {
                ScrollView {
                    LazyVGrid(columns: cols, spacing: 10) {
                        ForEach(model.pairs) { p in
                            pairCell(p)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }

            Divider()
            HStack {
                Text(model.summary).font(.caption).foregroundColor(.secondary)
                if !model.resultText.isEmpty {
                    Text(model.resultText).font(.caption).foregroundColor(.green)
                }
                Spacer()
                Button("全选") { model.picked = Set(model.pairs.map { $0.id }) }
                    .disabled(model.pairs.isEmpty)
                Button("全不选") { model.picked = [] }
                    .disabled(model.pairs.isEmpty)
                Button("导入 \(model.picked.count) 组", action: onImport)
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.picked.isEmpty)
            }
        }
        .padding(18)
        .frame(width: 640, height: 560)
    }

    @ViewBuilder
    private func pairCell(_ p: LivePair) -> some View {
        let on = model.picked.contains(p.id)
        Button {
            if on { model.picked.remove(p.id) } else { model.picked.insert(p.id) }
        } label: {
            VStack(spacing: 5) {
                ZStack(alignment: .topTrailing) {
                    if let img = p.thumb {
                        Image(nsImage: img)
                            .resizable().aspectRatio(contentMode: .fill)
                            .frame(width: 130, height: 130)
                            .clipped()
                            .cornerRadius(8)
                    } else {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color.secondary.opacity(0.15))
                            .frame(width: 130, height: 130)
                            .overlay(Image(systemName: "exclamationmark.triangle").foregroundColor(.orange))
                    }
                    Image(systemName: on ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 17))
                        .foregroundColor(on ? .accentColor : .white.opacity(0.85))
                        .background(Circle().fill(Color.black.opacity(0.25)).frame(width: 18, height: 18))
                        .padding(5)
                }
                Text(p.image.deletingPathExtension().lastPathComponent)
                    .font(.caption2).lineLimit(1)
            }
        }
        .buttonStyle(.plain)
        .opacity(on ? 1 : 0.55)
    }
}

// MARK: - 原生详情窗口（每个作品一个真正的 macOS 窗口）
/// 把 URL 作为 GURL Apple Event 发给已在运行的本机客户端（不会启动第二个实例）。
/// 必须在后台队列发送：TCC 自动化授权/目标 App 无响应会阻塞调用线程，放主线程就是卡死。
func sendGetURLEvent(_ url: URL, bundleId: String) {
    let target = NSAppleEventDescriptor(bundleIdentifier: bundleId)
    DispatchQueue.global(qos: .userInitiated).async {
        let event = NSAppleEventDescriptor(
            eventClass: AEEventClass(kInternetEventClass),
            eventID: AEEventID(kAEGetURL),
            targetDescriptor: target,
            returnID: AEReturnID(kAutoGenerateReturnID),
            transactionID: AETransactionID(kAnyTransactionID))
        event.setParam(NSAppleEventDescriptor(string: url.absoluteString), forKeyword: keyDirectObject)
        do { _ = try event.sendEvent(options: [.noReply], timeout: 3) } catch { }
    }
}

final class DetailWindows: NSObject, WKScriptMessageHandler {
    static let shared = DetailWindows()
    var windows: [NSWindow] = []

    /// 从窗口的 representedURL 解析出 (platform, item_id)
    static func keyFrom(_ w: NSWindow) -> (String, String)? {
        guard let rep = w.representedURL?.absoluteString,
              rep.hasPrefix("mediaarchive://item/") else { return nil }
        let rest = rep.replacingOccurrences(of: "mediaarchive://item/", with: "")
        let parts = rest.split(separator: "/", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return nil }
        return (parts[0], parts[1])
    }

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        // Origin validation: only accept messages from local server
        guard let url = message.webView?.url,
              url.host == "127.0.0.1" || url.host == "localhost" else { return }
        if message.name == "filterAuthor" {
            let author = (message.body as? [String: Any])?["author"] as? String ?? ""
            DispatchQueue.main.async {
                guard let d = NSApp.delegate as? AppDelegate else { return }
                d.openWindow()
                let enc = author.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? ""
                let apply: () -> Void = {
                    guard let web = WebHolder.main,
                          let u = URL(string: "http://127.0.0.1:8787/?author=\(enc)&t=\(Int(Date().timeIntervalSince1970))")
                    else { return }
                    var req = URLRequest(url: u)
                    req.cachePolicy = .reloadIgnoringLocalCacheData
                    web.load(req)
                }
                // 主窗口已有 WebView 就立刻刷；刚创建则等页面挂载完成
                if WebHolder.main != nil {
                    apply()
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.0, execute: apply)
                }
            }
            return
        }
        if message.name == "openURL" {
            if let d = message.body as? [String: Any], let str = d["url"] as? String,
               let u = URL(string: str), isWebURL(u) {
                NSApp.activate(ignoringOtherApps: true)   // 先把 App 唤醒到前台
                // 原帖优先交给本机客户端（小红书/抖音/Telegram…），没装再开浏览器
                if let ns = d["native"] as? String, !ns.isEmpty,
                   let nu = URL(string: ns), isNativeSchemeAllowed(nu) {
                    // douyin-pc 同时被 XMind 注册，默认处理器不一定是抖音；
                    // 这里显式指定 /Applications/抖音.app，并激活窗口。
                    if (nu.scheme ?? "").lowercased() == "douyin-pc" {
                        // 抖音 Mac 客户端对 douyin-pc:// 深链处理不稳，送 URL 会闪退；
                        // 这里只唤醒/启动 App，不送深链，同时用浏览器打开原帖/主页保底。
                        let bid = "com.bytedance.douyin.desktop"
                        let app = URL(fileURLWithPath: "/Applications/抖音.app")
                        if let running = NSRunningApplication.runningApplications(withBundleIdentifier: bid).first {
                            running.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
                        } else if FileManager.default.fileExists(atPath: app.path) {
                            let cfg = NSWorkspace.OpenConfiguration()
                            cfg.activates = true
                            NSWorkspace.shared.openApplication(at: app, configuration: cfg) { running, _ in
                                running?.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
                            }
                        }
                        NSWorkspace.shared.open(u)
                        return
                    }
                    if NSWorkspace.shared.urlForApplication(toOpen: nu) != nil {
                        NSWorkspace.shared.open(nu)
                    } else {
                        NSWorkspace.shared.open(u)
                    }
                } else {
                    NSWorkspace.shared.open(u)
                }
            }
            return
        }
        if message.name == "openSettings" {
            (NSApp.delegate as? AppDelegate)?.openSettings()
            return
        }
        if message.name == "importLive" {
            let dir = (message.body as? [String: Any])?["dir"] as? String
            if isUnderArchive(dir) {
                LiveImport.shared.run(dir: dir)
            } else {
                LiveImport.alert("目录不在归档内", "只能导入归档目录里的作品：\(dir ?? "")")
            }
            return
        }
        guard let body = message.body as? [String: Any], let raw = body["id"] else { return }
        let platform = (body["platform"] as? String) ?? ""
        let itemKey = (body["item_id"] as? String) ?? ""
        open(id: String(describing: raw),
             title: (body["title"] as? String) ?? "详情",
             platform: platform, itemKey: itemKey)
    }

    func open(id: String, title: String, platform: String = "", itemKey: String = "") {
        // 窗口身份用业务键（platform/item_id），不受自增 id 变化影响
        let winKey = (platform.isEmpty || itemKey.isEmpty) ? "id/\(id)" : "\(platform)/\(itemKey)"
        if let existed = windows.first(where: { $0.representedURL?.absoluteString == "mediaarchive://item/\(winKey)" }) {
            existed.makeKeyAndOrderFront(nil)
            return
        }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 780),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.title = ""
        w.titleVisibility = .hidden        // 标题改在页面第一行显示，标题栏不重复
        w.representedURL = URL(string: "mediaarchive://item/\(winKey)")
        w.center()
        w.isReleasedWhenClosed = false
        w.collectionBehavior = [.fullScreenPrimary]

        let cfg = WKWebViewConfiguration()
        cfg.preferences.isElementFullscreenEnabled = true
        cfg.allowsAirPlayForMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        let ucc = WKUserContentController()
        ucc.add(DetailWindows.shared, name: "importLive")
        ucc.add(DetailWindows.shared, name: "openURL")
        ucc.add(DetailWindows.shared, name: "filterAuthor")
        cfg.userContentController = ucc
        let web = WKWebView(frame: w.contentView!.bounds, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        // NOTE: drawsBackground is a private KVC key; no public API alternative exists on all macOS versions.
        // This may cause App Store rejection if submitted. For direct distribution it is safe.
        web.setValue(false, forKey: "drawsBackground")
        // 窗口外观跟随当前主题
        let curTheme = (Config.load()["theme"] as? String) ?? "default"
        let winSkin = ThemeSkin.of(curTheme)
        w.appearance = NSAppearance(named: winSkin.isDark ? .darkAqua : .aqua)
        w.backgroundColor = winSkin.bg
        if let u = URL(string: "http://127.0.0.1:8787/?detail=\(id)") {
            var req = URLRequest(url: u)
            req.cachePolicy = .reloadIgnoringLocalCacheData
            web.load(req)
        }
        w.contentView = web
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        windows.append(w)

        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification,
                                               object: w, queue: .main) { [weak self] _ in
            if let web = w.contentView as? WKWebView {
                web.evaluateJavaScript("""
                    document.querySelectorAll('video,audio').forEach(function(m){
                        try{ m.pause(); m.removeAttribute('src'); m.load(); }catch(e){}
                    });
                """, completionHandler: nil)
                web.stopLoading()
                web.configuration.userContentController.removeAllScriptMessageHandlers()
            }
            w.contentView = nil
            self?.windows.removeAll { $0 === w }
        }
    }
}

enum API {
    static let base = "http://127.0.0.1:8787"
    static func get(_ path: String, _ cb: @escaping ([String: Any]?) -> Void) {
        guard let u = URL(string: base + path) else { cb(nil); return }
        URLSession.shared.dataTask(with: u) { d, resp, _ in
            if let httpResp = resp as? HTTPURLResponse, httpResp.statusCode >= 400 {
                DispatchQueue.main.async { cb(nil) }
                return
            }
            var out: [String: Any]? = nil
            if let d = d { out = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] }
            DispatchQueue.main.async { cb(out) }
        }.resume()
    }
    static func post(_ path: String, _ body: [String: Any], _ cb: (([String: Any]?) -> Void)? = nil) {
        guard let u = URL(string: base + path) else { cb?(nil); return }
        var req = URLRequest(url: u)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { d, resp, _ in
            if let httpResp = resp as? HTTPURLResponse, httpResp.statusCode >= 400 {
                DispatchQueue.main.async { cb?(nil) }
                return
            }
            var out: [String: Any]? = nil
            if let d = d { out = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] }
            DispatchQueue.main.async { cb?(out) }
        }.resume()
    }
}

/// 读配置判断某类通知是否开启
func notifyOn(_ key: String, default def: Bool = true) -> Bool {
    let n = (Config.load()["notify"] as? [String: Any]) ?? [:]
    return (n[key] as? Bool) ?? def
}

/// 主题对应的原生窗口配色
struct ThemeSkin {
    let isDark: Bool
    let bg: NSColor
    let label: NSColor
    let secondaryLabel: NSColor

    static func of(_ theme: String) -> ThemeSkin {
        switch theme {
        case "deep":
            return ThemeSkin(isDark: true,
                             bg: NSColor(srgbRed: 0.067, green: 0.067, blue: 0.075, alpha: 1),
                             label: NSColor(srgbRed: 0.898, green: 0.898, blue: 0.906, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.443, green: 0.443, blue: 0.478, alpha: 1))
        case "twilight":
            return ThemeSkin(isDark: true,
                             bg: NSColor(srgbRed: 0.102, green: 0.063, blue: 0.145, alpha: 1),
                             label: NSColor(srgbRed: 0.929, green: 0.878, blue: 0.831, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.541, green: 0.439, blue: 0.565, alpha: 1))
        case "mint":
            return ThemeSkin(isDark: false,
                             bg: NSColor(srgbRed: 0.941, green: 0.992, blue: 0.973, alpha: 1),
                             label: NSColor(srgbRed: 0.059, green: 0.180, blue: 0.137, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.373, green: 0.541, blue: 0.478, alpha: 1))
        case "latte":
            return ThemeSkin(isDark: false,
                             bg: NSColor(srgbRed: 0.980, green: 0.965, blue: 0.945, alpha: 1),
                             label: NSColor(srgbRed: 0.231, green: 0.176, blue: 0.125, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.549, green: 0.478, blue: 0.408, alpha: 1))
        case "indigo":
            return ThemeSkin(isDark: false,
                             bg: NSColor(srgbRed: 0.961, green: 0.961, blue: 1.0, alpha: 1),
                             label: NSColor(srgbRed: 0.118, green: 0.106, blue: 0.294, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.388, green: 0.400, blue: 0.627, alpha: 1))
        case "coral":
            return ThemeSkin(isDark: false,
                             bg: NSColor(srgbRed: 1.0, green: 0.973, blue: 0.965, alpha: 1),
                             label: NSColor(srgbRed: 0.176, green: 0.122, blue: 0.102, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.549, green: 0.420, blue: 0.373, alpha: 1))
        case "glacier":
            return ThemeSkin(isDark: false,
                             bg: NSColor(srgbRed: 0.941, green: 0.976, blue: 1.0, alpha: 1),
                             label: NSColor(srgbRed: 0.047, green: 0.176, blue: 0.282, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.357, green: 0.541, blue: 0.647, alpha: 1))
        case "aurora":
            return ThemeSkin(isDark: true,
                             bg: NSColor(srgbRed: 0.047, green: 0.071, blue: 0.125, alpha: 1),
                             label: NSColor(srgbRed: 0.878, green: 0.925, blue: 0.957, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.353, green: 0.478, blue: 0.580, alpha: 1))
        case "graphite":
            return ThemeSkin(isDark: true,
                             bg: NSColor(srgbRed: 0.055, green: 0.055, blue: 0.063, alpha: 1),
                             label: NSColor(srgbRed: 0.831, green: 0.831, blue: 0.847, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.361, green: 0.361, blue: 0.388, alpha: 1))
        case "auto":
            let dark = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            return dark ? ThemeSkin.of("deep") : ThemeSkin.of("default")
        default:
            return ThemeSkin(isDark: false,
                             bg: NSColor(srgbRed: 0.957, green: 0.961, blue: 0.969, alpha: 1),
                             label: NSColor(srgbRed: 0.122, green: 0.137, blue: 0.161, alpha: 1),
                             secondaryLabel: NSColor(srgbRed: 0.545, green: 0.576, blue: 0.616, alpha: 1))
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var autoItem: NSMenuItem!
    var recentMenu: NSMenu!
    var watchTimer: Timer?
    var lastChangeCount = NSPasteboard.general.changeCount
    var lastClipboardTrigger: Date = .distantPast
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
    var menuKeys: [String: [String: Any]] = [:]   // importAll / extractAudio
    var menuRecording: String?
    var previewWindow: NSWindow?
    var didAutoReload = false
    var lastReloadTime: Date = .distantPast
    var lpAllItem: NSMenuItem?
    var exAudioItem: NSMenuItem?
    var lpWinItem: NSMenuItem?
    var cfgWatcher: Timer?
    var lastCfgStamp: TimeInterval = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        NSApp.setActivationPolicy(.regular)
        // 启动就把主题套到主窗口（标题栏、边框）
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            let t = (Config.load()["theme"] as? String) ?? "default"
            self.applyWindowTheme(t)
        }
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
        let diagDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".media-archive")
        try? FileManager.default.createDirectory(at: diagDir, withIntermediateDirectories: true)
        let diagPath = diagDir.appendingPathComponent("diag.txt").path
        let diag = "item=\(statusItem != nil) btn=\(statusItem.button != nil) title=\(statusItem.button?.title ?? "nil") visible=\(statusItem.isVisible) len=\(statusItem.length)\n"
        try? diag.write(toFile: diagPath, atomically: true, encoding: .utf8)
        buildMainMenu()
        buildMenu()
        applyStoredHotkey()
        applyMenuHotkeys()

        // 启动时恢复自动监听状态（配置里开着却没启动是之前的 bug）
        let startupCfg = Config.load()
        let aw = (startupCfg["auto_watch"] as? Bool)
                 ?? UserDefaults.standard.bool(forKey: DEFAULTS_KEY)
        UserDefaults.standard.set(aw, forKey: DEFAULTS_KEY)
        setAutoWatch(aw)
        startConfigWatcher()
        if UserDefaults.standard.bool(forKey: DEFAULTS_KEY) {
            setAutoWatch(true)
        }
        // Only auto-open window on first launch; subsequent launches respect user preference
        let hasOpenedBefore = UserDefaults.standard.bool(forKey: "ma_has_opened")
        if !hasOpenedBefore {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.openWindow() }
            UserDefaults.standard.set(true, forKey: "ma_has_opened")
        }
    }

    // MARK: 菜单
    func buildMenu() {
        let menu = NSMenu()

        let winItem = NSMenuItem(title: "打开主窗口", action: #selector(openWindow), keyEquivalent: "o")
        menu.addItem(winItem)
        let lpItem = NSMenuItem(title: "导入实况照片到相册…", action: #selector(importLivePhotosAction), keyEquivalent: "")
        menu.addItem(lpItem)
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

    var recentCache: [String] = []
    var recentCacheTime: Date = .distantPast

    @objc func refreshRecent() {
        // Use cached items if fetched within last 10 seconds
        if Date().timeIntervalSince(recentCacheTime) < 10 && !recentCache.isEmpty {
            updateRecentMenu(with: recentCache)
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
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
            self.recentCache = lines
            self.recentCacheTime = Date()
            DispatchQueue.main.async {
                self.updateRecentMenu(with: lines)
            }
        }
    }

    private func updateRecentMenu(with lines: [String]) {
        recentMenu.removeAllItems()
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
    func application(_ application: NSApplication, open urls: [URL]) {
        for u in urls {
            if u.scheme == "mediaarchive" {
                switch u.host {
                case "settings": openSettings()
                case "import":
                    let comps = URLComponents(url: u, resolvingAgainstBaseURL: false)
                    let d = comps?.queryItems?.first(where: { $0.name == "dir" })?.value
                    if isUnderArchive(d) {
                        LiveImport.shared.run(dir: d)
                    } else {
                        LiveImport.alert("目录不在归档内", "只能导入归档目录里的作品：\(d ?? "")")
                    }
                case "open":
                    let comps = URLComponents(url: u, resolvingAgainstBaseURL: false)
                    if let str = comps?.queryItems?.first(where: { $0.name == "url" })?.value,
                       let t = URL(string: str), isWebURL(t) {
                        NSApp.activate(ignoringOtherApps: true)
                        if let ns = comps?.queryItems?.first(where: { $0.name == "native" })?.value,
                           !ns.isEmpty, let nu = URL(string: ns), isNativeSchemeAllowed(nu),
                           NSWorkspace.shared.urlForApplication(toOpen: nu) != nil {
                            NSWorkspace.shared.open(nu)
                        } else {
                            NSWorkspace.shared.open(t)
                        }
                    }
                case "window", .some: openWindow()
                case .none: openWindow()
                }
            }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { openWindow() }
        return true
    }

    @objc func noop() {}

    
struct WebPanel: NSViewRepresentable {
    let url: URL
    func makeNSView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.allowsAirPlayForMediaPlayback = true
        cfg.mediaTypesRequiringUserActionForPlayback = []
        // 关键：允许 <video> 走元素全屏（否则 WKWebView 里没有全屏按钮）
        if #available(macOS 10.12, *) {
            cfg.preferences.isElementFullscreenEnabled = true
        }
        let ucc = WKUserContentController()
        ucc.add(DetailWindows.shared, name: "openDetail")
        ucc.add(DetailWindows.shared, name: "importLive")
        ucc.add(DetailWindows.shared, name: "openSettings")
        ucc.add(DetailWindows.shared, name: "openURL")
        ucc.add(DetailWindows.shared, name: "filterAuthor")
        cfg.userContentController = ucc
        let w = WKWebView(frame: .zero, configuration: cfg)
        var req = URLRequest(url: url)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.timeoutInterval = 30
        w.load(req)
        WebHolder.main = w
        return w
    }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

struct MainView: View {
    var body: some View {
        // 固定 URL：时间戳会让 SwiftUI 每次重算 body 都换 URL，进而整页重载
        // 首次加载的 no-store + reloadIgnoringLocalCacheData 已足够避免旧页面
        WebPanel(url: URL(string: "http://127.0.0.1:8787/")!)
    }
}

// MARK: - 设置窗口
    // MARK: - 导入实况照片到系统相册
    @objc func importLivePhotosAction() {
        LiveImport.shared.runAll()
    }

    @objc func extractAudioAction() {
        guard let w = NSApp.keyWindow, let (pf, key) = DetailWindows.keyFrom(w) else {
            LiveImport.alert("没有打开的详情窗口", "先打开一个视频作品，再按这个快捷键。")
            return
        }
        let runWith: (Int) -> Void = { iid in
            guard iid > 0 else { return }

            API.post("/api/extract_audio", ["item_id": iid]) { r in
                if (r?["ok"] as? Bool) ?? false {
                    let f = (r?["file"] as? String) ?? "audio"
                    let sz = ((r?["size"] as? Double) ?? 0) / 1048576
                    if notifyOn("audio") {
                        self.notify("音频已提取", String(format: "%@（%.1f MB）", f, sz))
                    }
                } else {
                    LiveImport.alert("提取失败", (r?["error"] as? String) ?? "未知错误")
                }
            }
        }
        if pf == "id" {
            runWith(Int(key) ?? 0)
        } else {
            API.get("/api/item_folder_by_key?platform=\(pf)&item_id=\(key)") { r in
                runWith((r?["id"] as? NSNumber)?.intValue ?? 0)
            }
        }
    }

    @objc func importLiveFromCurrentWindow() {
        guard let w = NSApp.keyWindow, let (pf, key) = DetailWindows.keyFrom(w) else {
            LiveImport.alert("没有打开的详情窗口",
                             "先打开一条作品（双击卡片），再按快捷键。")
            return
        }
        let go: (String) -> Void = { dir in
            if dir.isEmpty {
                LiveImport.alert("查不到这条作品的目录", "\(pf)/\(key)")
            } else {
                LiveImport.shared.run(dir: dir)
            }
        }
        if pf == "id" {
            API.get("/api/item_folder?id=\(key)") { r in
                go((r?["folder"] as? String) ?? "")
            }
        } else {
            API.get("/api/item_folder_by_key?platform=\(pf)&item_id=\(key)") { r in
                go((r?["folder"] as? String) ?? "")
            }
        }
    }

    @objc func importLivePhotosFromFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.prompt = "打开"
        panel.message = "选择包含实况照片（同名 .jpg + .mov）的文件夹"
        panel.directoryURL = URL(fileURLWithPath: currentArchiveDir())
        guard panel.runModal() == .OK, let dir = panel.url else { return }
        showLivePreview(for: dir)
    }

    func showLivePreview(for dir: URL) {
        if let old = previewWindow { old.close(); previewWindow = nil }
        let model = LivePreviewModel(folder: dir)
        let view = LivePreviewView(
            model: model,
            onImport: {
                let chosen = model.pairs.filter { model.picked.contains($0.id) }.map { ($0.image, $0.video) }
                guard !chosen.isEmpty else { return }
                model.resultText = "导入中…"
                LiveImport.shared.importSelected(chosen) { ok, total, errs in
                    model.resultText = "成功 \(ok)/\(total) 组"
                    if ok < total {
                        LiveImport.alert("部分失败", errs.joined(separator: "\n"))
                    } else {
                        if notifyOn("livephoto") {
                            LiveImport.notify("导入完成", "成功 \(ok) 组实况照片")
                        }
                    }
                }
            },
            onClose: { [weak self] in
                self?.previewWindow?.close()
                self?.previewWindow = nil
            })
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 560),
                         styleMask: [.titled, .closable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "导入实况照片"
        w.contentView = NSHostingView(rootView: view)
        w.center()
        w.isReleasedWhenClosed = false
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        previewWindow = w
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification,
                                               object: w, queue: .main) { [weak self] _ in
            if self?.previewWindow === w { self?.previewWindow = nil }
        }
    }

    // MARK: - 配置热更新（网页改了快捷键/目录，App 自动跟上）
    // TODO: Replace Timer-based mtime polling with DispatchSource.makeFileSystemEventSource
    // for more efficient and responsive config change detection.
    func startConfigWatcher() {
        lastCfgStamp = cfgStamp()
        cfgWatcher = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let st = self.cfgStamp()
            if st != self.lastCfgStamp {
                self.lastCfgStamp = st
                self.applyStoredHotkey()      // 静默生效，不弹通知
            }
            self.pollImportQueue()
        }
    }

    /// 网页点「导入实况照片」→ 后端写队列 → 这里取走执行
    func pollImportQueue() {
        let q = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".media-archive").appendingPathComponent("import_queue.json")
        guard FileManager.default.fileExists(atPath: q.path),
              let data = try? Data(contentsOf: q),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let dir = obj["folder"] as? String, !dir.isEmpty else { return }
        // Execute first, then delete queue file only on success
        // 队列里可能还是 01.jpg + 01_live.mp4，先走完整流程（自动合成 .mov）再导
        if isUnderArchive(dir) {
            try? FileManager.default.removeItem(at: q)
            LiveImport.shared.run(dir: dir)
        } else {
            // Invalid directory — don't delete the queue entry so it can be retried or inspected
            LiveImport.alert("目录不在归档内", "只能导入归档目录里的作品：\(dir)")
        }
    }

    func cfgStamp() -> TimeInterval {
        let attrs = try? FileManager.default.attributesOfItem(atPath: Config.url.path)
        return (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
    }

    // MARK: - 主菜单（⌘C/⌘V/⌘A 依赖这里的标准 selector）
    func buildMainMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "关于 媒体归档", action: nil, keyEquivalent: ""))
        appMenu.addItem(NSMenuItem.separator())
        let lpAll = NSMenuItem(title: "一键导入所有实况照片到相册…",
                               action: #selector(importLivePhotosAction), keyEquivalent: "i")
        lpAll.target = self
        lpAll.tag = 1001
        appMenu.addItem(lpAll)
        lpAllItem = lpAll

        let lpWin = NSMenuItem(title: "对当前窗口提取实况照片…",
                               action: #selector(importLiveFromCurrentWindow), keyEquivalent: "i")
        lpWin.target = self
        lpWin.tag = 1003
        appMenu.addItem(lpWin)
        lpWinItem = lpWin

        let exAudio = NSMenuItem(title: "提取当前窗口作品的音频…",
                                 action: #selector(extractAudioAction), keyEquivalent: "e")
        exAudio.target = self
        exAudio.tag = 1002
        appMenu.addItem(exAudio)
        exAudioItem = exAudio
        appMenu.addItem(NSMenuItem.separator())
        let setMain = NSMenuItem(title: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        setMain.target = self
        appMenu.addItem(setMain)
        appMenu.addItem(NSMenuItem.separator())
        let hideItem = NSMenuItem(title: "隐藏", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(hideItem)
        appMenu.addItem(NSMenuItem(title: "退出 媒体归档", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        edit.addItem(NSMenuItem(title: "撤销", action: Selector(("undo:")), keyEquivalent: "z"))
        edit.addItem(NSMenuItem(title: "重做", action: Selector(("redo:")), keyEquivalent: "Z"))
        edit.addItem(NSMenuItem.separator())
        edit.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        edit.addItem(NSMenuItem(title: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        edit.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "窗口")
        let reloadItem = NSMenuItem(title: "刷新页面", action: #selector(reloadWeb), keyEquivalent: "r")
        reloadItem.target = self
        view.addItem(reloadItem)
        view.addItem(NSMenuItem.separator())
        let mainWin = NSMenuItem(title: "打开主窗口 / 在 Finder 显示", action: #selector(openOrReveal), keyEquivalent: "o")
        mainWin.target = self
        view.addItem(mainWin)
        view.addItem(NSMenuItem.separator())
        view.addItem(NSMenuItem(title: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
        view.addItem(NSMenuItem(title: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: ""))
        view.addItem(NSMenuItem.separator())
        view.addItem(NSMenuItem(title: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
        let fsItem = NSMenuItem(title: "进入全屏", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fsItem.keyEquivalentModifierMask = [.command, .control]
        view.addItem(fsItem)
        viewItem.submenu = view
        main.addItem(viewItem)
        NSApp.windowsMenu = view

        NSApp.mainMenu = main
    }

    @objc func openSettings() {
        // 永远重建：旧窗口里的 SwiftUI 视图是创建时的代码，不重建就会看到过时界面
        if let w = settingsWindow {
            w.orderOut(nil)
            w.close()
            settingsWindow = nil
        }
        // 兜底：关掉所有残留的设置窗口
        for w in NSApp.windows where w.title == "设置" {
            w.orderOut(nil)
            w.close()
        }
        settingsModel = SettingsModel()
        settingsModel.archiveDir = currentArchiveDir()
        settingsModel.hotkeyText = hotkeyDisplay
        settingsModel.autoWatch = UserDefaults.standard.bool(forKey: DEFAULTS_KEY)
        let cc = Config.load()
        if let wp = (cc["watch_platforms"] as? [String]) {
            settingsModel.watchPlatforms = wp
        }
        settingsModel.theme = (cc["theme"] as? String) ?? "auto"
        if let nt = (cc["notify"] as? [String: Any]) {
            settingsModel.notifyStart = (nt["start"] as? Bool) ?? true
            settingsModel.notifyDone = (nt["done"] as? Bool) ?? true
            settingsModel.notifyExists = (nt["exists"] as? Bool) ?? true
            settingsModel.notifyError = (nt["error"] as? Bool) ?? true
            settingsModel.notifyAudio = (nt["audio"] as? Bool) ?? true
            settingsModel.notifyLive = (nt["livephoto"] as? Bool) ?? true
            settingsModel.notifyRefresh = (nt["refresh"] as? Bool) ?? false
            settingsModel.notifySettings = (nt["settings"] as? Bool) ?? false
        }
        settingsModel.webStatus = "正在检查…"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let alive = self?.webAlive() ?? false
            DispatchQueue.main.async {
                self?.settingsModel.webStatus = alive ? "运行中 · 127.0.0.1:8787" : "未运行"
            }
        }
        pendingHotkey = [:]
        loadSettingsData()

        let view = SettingsView(model: settingsModel,
            onPick: { [weak self] in self?.pickArchiveDir() },
            onOpenDir: { [weak self] in self?.openArchive() },
            onRecord: { [weak self] in self?.toggleRecording() },
            onOpenWeb: { [weak self] in self?.openWeb() },
            onRestartWeb: { [weak self] in self?.restartWeb() },
            onRecordMenu: { [weak self] key in self?.toggleMenuRecording(key) },
            onSave: { [weak self] in
                self?.saveSettings()
                self?.settingsWindow?.close()
                self?.settingsWindow = nil
            },
            onClose: { [weak self] in
                self?.settingsWindow?.close()
                self?.settingsWindow = nil
            })

        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 640),
                         styleMask: [.titled, .closable],
                         backing: .buffered, defer: false)
        w.title = "设置"
        let skin = ThemeSkin.of(settingsModel.theme)
        let host = NSHostingView(rootView: view)
        host.wantsLayer = true
        host.layer?.backgroundColor = skin.bg.cgColor
        w.contentView = host
        w.appearance = NSAppearance(named: skin.isDark ? .darkAqua : .aqua)
        w.backgroundColor = skin.bg
        w.center()
        w.isReleasedWhenClosed = false
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        settingsWindow = w

        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification,
                                               object: w, queue: .main) { [weak self] _ in
            if self?.settingsWindow === w { self?.settingsWindow = nil }
        }
    }

    func loadSettingsData() {
        let grp = DispatchGroup()

        grp.enter()
        API.get("/api/config") { [weak self] c in
            defer { grp.leave() }
            guard let self = self, let c = c else { return }
            if let lp = c["livephoto"] as? [String: Any], let m = lp["mode"] as? String {
                self.settingsModel.livephotoMode = m
            }
            if let d = c["archive_dir"] as? String, !d.isEmpty {
                self.settingsModel.archiveDir = d
            }
        }
        let cfg = Config.load()
        settingsModel.ytdlpBrowserCookies = (cfg["ytdlp_use_browser_cookies"] as? Bool) ?? false
        if let mh = (cfg["menu_hotkeys"] as? [String: Any]) {
            menuKeys = (mh as? [String: [String: Any]]) ?? [:]
            if let d = menuKeys["importAll"]?["display"] as? String { settingsModel.importAllKey = d }
            if let d = menuKeys["extractAudio"]?["display"] as? String { settingsModel.extractKey = d }
            if let d = menuKeys["importFolder"]?["display"] as? String { settingsModel.importFolderKey = d }
        }
        // 请求回来后立刻记录快照，精确、无闪烁
        grp.notify(queue: .main) { [weak self] in
            self?.settingsModel.markSaved()
        }
    }

    /// 把主题应用到所有原生窗口（设置窗、详情窗的标题栏）
    func applyWindowTheme(_ theme: String, windows: [NSWindow]? = nil) {
        let skin = ThemeSkin.of(theme)
        let appearance = NSAppearance(named: skin.isDark ? .darkAqua : .aqua)
        var targets = (windows ?? [settingsWindow, mainWindow].compactMap { $0 })
        targets += DetailWindows.shared.windows
        for w in targets {
            w.appearance = appearance
            w.backgroundColor = skin.bg
            if let web = w.contentView as? WKWebView {
                // NOTE: drawsBackground is private KVC; safe for direct distribution, App Store risk.
                web.setValue(false, forKey: "drawsBackground")
            }
        }
    }

    /// 保存后立刻把主题推到所有已打开的页面，不用刷新
    func applyThemeNow(_ theme: String) {
        let js = "document.documentElement.setAttribute('data-theme','\(theme)')"
        WebHolder.main?.evaluateJavaScript(js, completionHandler: nil)
        for w in DetailWindows.shared.windows {
            (w.contentView as? WKWebView)?.evaluateJavaScript(js, completionHandler: nil)
        }
    }

    func saveSettings() {
        var cfg = Config.load()
        cfg["archive_dir"] = settingsModel.archiveDir
        if !pendingHotkey.isEmpty { cfg["hotkey"] = pendingHotkey }
        cfg["auto_watch"] = settingsModel.autoWatch
        cfg["watch_platforms"] = settingsModel.watchPlatforms
        cfg["theme"] = settingsModel.theme
        cfg["notify"] = ["start": settingsModel.notifyStart,
                         "done": settingsModel.notifyDone,
                         "exists": settingsModel.notifyExists,
                         "error": settingsModel.notifyError,
                         "audio": settingsModel.notifyAudio,
                         "livephoto": settingsModel.notifyLive,
                         "refresh": settingsModel.notifyRefresh,
                         "settings": settingsModel.notifySettings]
        cfg["livephoto"] = ["mode": settingsModel.livephotoMode]
        cfg["ytdlp_use_browser_cookies"] = settingsModel.ytdlpBrowserCookies
        if !menuKeys.isEmpty { cfg["menu_hotkeys"] = menuKeys }
        if !Config.save(cfg) {
            alert("保存失败", "配置文件写入失败，请检查磁盘空间和权限。")
            return
        }
        UserDefaults.standard.set(settingsModel.autoWatch, forKey: DEFAULTS_KEY)
        setAutoWatch(settingsModel.autoWatch)
        applyStoredHotkey()
        // 不再重启 Web 服务：配置每次都是现读的，重启只会让页面断连卡顿
        applyThemeNow(settingsModel.theme)   // 网页立刻生效
        applyWindowTheme(settingsModel.theme)   // 原生窗口也跟着变
        settingsModel.markSaved()      // 记住这次的值，保存按钮随之变灰
        if notifyOn("settings", default: false) { notify("设置已保存", "已生效") }
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

    func toggleMenuRecording(_ key: String) {
        if menuRecording != nil { stopMenuRecording(); return }
        menuRecording = key
        settingsModel.recording = true
        settingsModel.recordingTarget = key
        recordingMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] ev in
            guard let self = self, let target = self.menuRecording else { return ev }
            let mods = ev.modifierFlags.intersection([.command, .option, .control, .shift])
            if mods.isEmpty { NSSound.beep(); return nil }
            var names: [String] = []
            if mods.contains(.control) { names.append("control") }
            if mods.contains(.option) { names.append("option") }
            if mods.contains(.shift) { names.append("shift") }
            if mods.contains(.command) { names.append("command") }
            let ch = (ev.charactersIgnoringModifiers ?? "").lowercased()
            guard !ch.isEmpty, ch.rangeOfCharacter(from: .alphanumerics) != nil else {
                NSSound.beep(); return nil
            }
            let disp = self.displayFor(names: names, key: ch.uppercased())
            self.menuKeys[target] = ["key": ch, "modifiers": names, "display": disp]
            if target == "importAll" {
                self.settingsModel.importAllKey = disp
                self.applyMenuKey(self.lpAllItem, key: "importAll", fallback: "I", mask: [.command, .shift])
            } else if target == "importFolder" {
                self.settingsModel.importFolderKey = disp
                self.applyMenuKey(self.lpWinItem, key: "importFolder", fallback: "i", mask: [.command])
            } else {
                self.settingsModel.extractKey = disp
                self.applyMenuKey(self.exAudioItem, key: "extractAudio", fallback: "e")
            }
            self.stopMenuRecording()
            return nil
        }
    }

    func stopMenuRecording() {
        menuRecording = nil
        settingsModel.recording = false
        settingsModel.recordingTarget = ""
        if let m = recordingMonitor { NSEvent.removeMonitor(m); recordingMonitor = nil }
    }

    func toggleRecording() {
        if settingsModel.recording { stopRecording(); return }
        settingsModel.recording = true
        settingsModel.recordingTarget = "global"
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
        settingsModel.recordingTarget = ""
        if let m = recordingMonitor {
            NSEvent.removeMonitor(m)
            recordingMonitor = nil
        }
        if pendingHotkey["display"] as? String == nil {
            settingsModel.hotkeyText = hotkeyDisplay
        }
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
            DispatchQueue.global(qos: .utility).async { [weak self] in
                let alive = self?.webAlive() ?? false
                DispatchQueue.main.async {
                    self?.settingsModel.webStatus = alive
                        ? "运行中 · http://127.0.0.1:8787" : "未运行"
                }
            }
        }
    }

    func applicationDidBecomeActive(_ note: Notification) {
        // Only reload if page hasn't been loaded in last 30 seconds
        let now = Date()
        if now.timeIntervalSince(lastReloadTime) > 30 {
            lastReloadTime = now
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                guard let web = WebHolder.main else { return }
                let ts = Int(Date().timeIntervalSince1970)
                if let u = URL(string: "http://127.0.0.1:8787/?v=\(ts)") {
                    var req = URLRequest(url: u)
                    req.cachePolicy = .reloadIgnoringLocalCacheData
                    web.load(req)
                }
            }
        }
    }

    /// ⌘O：详情窗口里 → 在 Finder 显示该作品；否则 → 打开主窗口
    @objc func openOrReveal() {
        if let w = NSApp.keyWindow, let (pf, key) = DetailWindows.keyFrom(w) {
            if pf == "id" {
                API.get("/api/item_folder?id=\(key)") { r in
                    if let dir = r?["folder"] as? String, !dir.isEmpty {
                        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: dir)
                    }
                }
            } else {
                API.get("/api/item_folder_by_key?platform=\(pf)&item_id=\(key)") { r in
                    if let dir = r?["folder"] as? String, !dir.isEmpty {
                        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: dir)
                    }
                }
            }
            return
        }
        openWindow()
    }

    @objc func reloadWeb() {
        guard let web = WebHolder.main else {
            openWindow()
            return
        }
        var req = URLRequest(url: URL(string: "http://127.0.0.1:8787/?t=\(Int(Date().timeIntervalSince1970))")!)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        web.load(req)
        mainWindow?.makeKeyAndOrderFront(nil)
        if notifyOn("refresh", default: false) { notify("已刷新", "主窗口页面") }
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
        w.title = ""
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true
        // 主窗口也跟随主题
        let bootTheme = (Config.load()["theme"] as? String) ?? "default"
        let bootSkin = ThemeSkin.of(bootTheme)
        w.appearance = NSAppearance(named: bootSkin.isDark ? .darkAqua : .aqua)
        w.backgroundColor = bootSkin.bg
        w.center()
        w.isReleasedWhenClosed = false
        w.contentView = NSHostingView(rootView: MainView())
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

    @objc func quit() {
        NSApp.terminate(nil)
    }

    @objc func openArchive() {
        NSWorkspace.shared.open(URL(fileURLWithPath: currentArchiveDir()))
    }

    @objc func openIndex() {
        let f = URL(fileURLWithPath: currentArchiveDir()).appendingPathComponent("索引.csv")
        if FileManager.default.fileExists(atPath: f.path) {
            NSWorkspace.shared.open(f)
        } else {
            alert("找不到索引文件", "归档目录里还没有 索引.csv")
        }
    }

    @objc func openWeb() {
        let port = (Config.load()["web_port"] as? Int) ?? 8787
        if let u = URL(string: "http://127.0.0.1:\(port)") {
            NSWorkspace.shared.open(u)
        }
    }

    func watchLog(_ msg: String) {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".media-archive")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let f = dir.appendingPathComponent("watch.log")
        let line = "\(Date().timeIntervalSince1970) \(msg)\n"
        if let h = try? FileHandle(forWritingTo: f) {
            h.seekToEndOfFile()
            h.write(line.data(using: .utf8)!)
            try? h.close()
        } else {
            try? line.write(to: f, atomically: true, encoding: .utf8)
        }
    }

    func pollClipboard() {
        let pb = NSPasteboard.general
        let cc = pb.changeCount
        guard cc != lastChangeCount else { return }
        // Debounce: require at least 500ms since last trigger
        let now = Date()
        guard now.timeIntervalSince(lastClipboardTrigger) >= 0.5 else { return }
        lastChangeCount = cc
        lastClipboardTrigger = now
        let text = pb.string(forType: .string) ?? ""
        watchLog("change cc=\(cc) text=\(text.prefix(60))")
        guard let url = extractURL(text) else { watchLog("  no url"); return }
        if url == lastHandled { watchLog("  dup, skip"); return }
        guard shouldHandle(url) else { watchLog("  host not allowed: \(url)"); return }
        lastHandled = url
        watchLog("  ARCHIVE \(url)")
        archive(url)
    }

    func extractURL(_ text: String) -> String? {
        let pattern = "https?://[^\\s\"'<>，。、）)】]+"
        guard let re = try? NSRegularExpression(pattern: pattern) else { return nil }
        let ns = text as NSString
        guard let m = re.firstMatch(in: text, range: NSRange(location: 0, length: ns.length)) else { return nil }
        return ns.substring(with: m.range)
    }

    func shouldHandle(_ url: String) -> Bool {
        guard let host = URL(string: url)?.host?.lowercased() else { return false }
        let cfg = Config.load()
        let enabled = Set((cfg["watch_platforms"] as? [String]) ?? ALL_PLATFORM_IDS)
        let hosts = PLATFORMS.filter { enabled.contains($0.id) }.flatMap { $0.hosts }
        return hosts.contains { host == $0 || host.hasSuffix("." + $0) }
    }

    func setAutoWatch(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: DEFAULTS_KEY)
        autoItem.state = on ? .on : .off
        watchTimer?.invalidate()
        watchTimer = nil
        watchLog("setAutoWatch(\(on))")
        if on {
            lastChangeCount = NSPasteboard.general.changeCount
            watchTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
                self.pollClipboard()
            }
            RunLoop.main.add(watchTimer!, forMode: .common)
        }
    }

    @objc func toggleAuto() {
        setAutoWatch(!UserDefaults.standard.bool(forKey: DEFAULTS_KEY))
    }

    func archive(_ text: String) {
        guard let url = extractURL(text) else {
            alert("没找到链接", "剪贴板里没有可识别的链接。")
            return
        }
        running = true
        statusItem.button?.toolTip = "正在归档…"
        let ncfg = (Config.load()["notify"] as? [String: Any]) ?? [:]
        if (ncfg["start"] as? Bool) ?? true {
            notify("开始归档", url.prefix(60).description)
        }
        // 统一交给 Web 服务排队，这样主页的任务面板能显示进度
        API.post("/api/archive", ["url": url]) { [weak self] r in
            guard let self = self else { return }
            // 服务端返回 {"ok":true,"task":"..."}；只要拿到 task id 就说明排队成功
            let queued = (r?["task"] as? String) != nil || ((r?["ok"] as? Bool) ?? false)
            self.running = false
            self.statusItem.button?.toolTip = "媒体归档"
            if !queued {
                if notifyOn("error") {
                    self.notify("归档失败", (r?["error"] as? String) ?? "无法连接本地服务")
                }
            }
        }
    }

    var alertQueue: [(String, String)] = []
    var alertShowing = false

    func alert(_ title: String, _ text: String) {
        if alertShowing {
            alertQueue.append((title, text))
            return
        }
        alertShowing = true
        let a = NSAlert()
        a.messageText = title
        a.informativeText = text
        a.alertStyle = .informational
        a.addButton(withTitle: "好")
        a.runModal()
        alertShowing = false
        if let next = alertQueue.first {
            alertQueue.removeFirst()
            alert(next.0, next.1)
        }
    }

    func notify(_ title: String, _ text: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = text
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    // MARK: 全局快捷键
    func applyMenuHotkeys() {
        let cfg = Config.load()
        let m = (cfg["menu_hotkeys"] as? [String: Any]) ?? [:]
        menuKeys = (m as? [String: [String: Any]]) ?? [:]
        applyMenuKey(lpAllItem, key: "importAll", fallback: "I", mask: [.command, .shift])
        applyMenuKey(lpWinItem, key: "importFolder", fallback: "i", mask: [.command])
        applyMenuKey(exAudioItem, key: "extractAudio", fallback: "e", mask: [.command])
    }

    func applyMenuKey(_ item: NSMenuItem?, key: String, fallback: String,
                      mask: NSEvent.ModifierFlags = [.command]) {
        guard let item = item else { return }
        if let d = menuKeys[key], let k = d["key"] as? String, !k.isEmpty {
            item.keyEquivalent = k
            item.keyEquivalentModifierMask = carbonMaskToCocoa((d["modifiers"] as? [String]) ?? ["command"])
        } else {
            item.keyEquivalent = fallback
            item.keyEquivalentModifierMask = mask
        }
    }

    func carbonMaskToCocoa(_ names: [String]) -> NSEvent.ModifierFlags {
        var f: NSEvent.ModifierFlags = []
        if names.contains("command") { f.insert(.command) }
        if names.contains("option") { f.insert(.option) }
        if names.contains("control") { f.insert(.control) }
        if names.contains("shift") { f.insert(.shift) }
        return f
    }

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
        // Only install the event handler once; re-registering the hotkey ref is sufficient
        AppDelegate.installHotKeyHandlerIfNeeded()
        _ = hotKeyID
    }

    private static var hotKeyHandlerInstalled = false
    private static func installHotKeyHandlerIfNeeded() {
        guard !hotKeyHandlerInstalled else { return }
        hotKeyHandlerInstalled = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { (_, event, _) -> OSStatus in
            DispatchQueue.main.async {
                (NSApp.delegate as? AppDelegate)?.archiveClipboard()
            }
            return noErr
        }, 1, &spec, nil, nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
