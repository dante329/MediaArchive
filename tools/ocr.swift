// 用 macOS Vision 做本地 OCR（中文优先，免费、离线）
// 用法： ocr <图片>...
import Foundation
import Vision
import AppKit

let files = Array(CommandLine.arguments.dropFirst())
guard !files.isEmpty else {
    FileHandle.standardError.write("用法: ocr <图片>...\n".data(using: .utf8)!)
    exit(2)
}

for f in files {
    guard let img = NSImage(contentsOfFile: f),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        FileHandle.standardError.write("跳过（无法读取）: \(f)\n".data(using: .utf8)!)
        continue
    }
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([req])
        print("=== \((f as NSString).lastPathComponent) ===")
        for obs in (req.results ?? []) {
            if let c = obs.topCandidates(1).first { print(c.string) }
        }
    } catch {
        FileHandle.standardError.write("识别失败 \(f): \(error)\n".data(using: .utf8)!)
    }
}
