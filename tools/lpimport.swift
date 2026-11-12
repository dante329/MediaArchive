// 把「图片 + MOV」配对导入系统相册，生成真正的实况照片（Live Photo）
// 用法： lpimport <图片> <视频>  |  lpimport --dir <目录>
import Foundation
import Photos

func importPair(_ img: URL, _ mov: URL, done: @escaping (Bool, String) -> Void) {
    PHPhotoLibrary.shared().performChanges({
        let req = PHAssetCreationRequest.forAsset()
        let opts = PHAssetResourceCreationOptions()
        req.addResource(with: .photo, fileURL: img, options: opts)
        req.addResource(with: .pairedVideo, fileURL: mov, options: opts)
    }) { ok, err in
        done(ok, err?.localizedDescription ?? "")
    }
}

let args = Array(CommandLine.arguments.dropFirst())
guard !args.isEmpty else {
    print("用法: lpimport <图片> <视频>  或  lpimport --dir <目录>")
    exit(2)
}

var pairs: [(URL, URL)] = []
if args[0] == "--dir", args.count > 1 {
    let dir = URL(fileURLWithPath: args[1])
    let files = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
    let jpgs = files.filter { ["jpg", "jpeg", "heic"].contains($0.pathExtension.lowercased()) }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    for img in jpgs {
        let mov = dir.appendingPathComponent(img.deletingPathExtension().lastPathComponent + ".mov")
        if FileManager.default.fileExists(atPath: mov.path) { pairs.append((img, mov)) }
    }
} else if args.count >= 2 {
    pairs.append((URL(fileURLWithPath: args[0]), URL(fileURLWithPath: args[1])))
}

guard !pairs.isEmpty else { print("没找到可导入的配对文件"); exit(2) }
print("待导入 \(pairs.count) 组实况照片")

let sem = DispatchSemaphore(value: 0)
var pending = pairs.count
PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
    guard status == .authorized || status == .limited else {
        print("✗ 没有相册写入权限（系统设置 → 隐私与安全性 → 照片）")
        exit(3)
    }
    for (img, mov) in pairs {
        importPair(img, mov) { ok, msg in
            let name = img.deletingPathExtension().lastPathComponent
            print(ok ? "  ✓ \(name)" : "  ✗ \(name) — \(msg)")
            pending -= 1
            if pending == 0 { sem.signal() }
        }
    }
}
_ = sem.wait(timeout: .now() + 120)
RunLoop.current.run(until: Date().addingTimeInterval(1.5))
