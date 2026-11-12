// 按 Apple 规范生成实况照片的两部分：
//   1) MOV：复制视频轨 + 新增 mdta/com.apple.quicktime.still-image-time 元数据轨
//   2) 图片：写入 ContentIdentifier（与 MOV 的 content identifier 一致）
// 用法： lpmake <图片> <视频> [输出目录]
import Foundation
import AVFoundation

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

enum Paths {
    static var makerNoteTemplate: String {
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        return exe.deletingLastPathComponent().appendingPathComponent("apple_makernote.bin").path
    }
}

let args = Array(CommandLine.arguments.dropFirst())
guard args.count >= 2 else { fail("用法: lpmake <图片> <视频> [输出目录]") }
let imgURL = URL(fileURLWithPath: args[0])
let srcURL = URL(fileURLWithPath: args[1])
let outDir = args.count > 2 ? URL(fileURLWithPath: args[2])
                            : imgURL.deletingLastPathComponent()
let stem = imgURL.deletingPathExtension().lastPathComponent
let identifier = UUID().uuidString

// ---------- 1) 重建 MOV ----------
let asset = AVURLAsset(url: srcURL)
guard let vTrack = asset.tracks(withMediaType: .video).first else { fail("源视频没有视频轨") }

let reader = try! AVAssetReader(asset: asset)
let vOut = AVAssetReaderTrackOutput(track: vTrack, outputSettings: nil)
reader.add(vOut)

let movURL = outDir.appendingPathComponent(stem + ".mov")
try? FileManager.default.removeItem(at: movURL)
let writer = try! AVAssetWriter(outputURL: movURL, fileType: .mov)

let vIn = AVAssetWriterInput(mediaType: .video, outputSettings: nil)
vIn.expectsMediaDataInRealTime = false
writer.add(vIn)

// 元数据轨规格：mdta/com.apple.quicktime.still-image-time, SInt8
let spec: [String: Any] = [
    kCMMetadataFormatDescriptionMetadataSpecificationKey_Identifier as String:
        "mdta/com.apple.quicktime.still-image-time",
    kCMMetadataFormatDescriptionMetadataSpecificationKey_DataType as String:
        kCMMetadataBaseDataType_SInt8 as String
]
var fmtDesc: CMFormatDescription?
let st = CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
    allocator: kCFAllocatorDefault,
    metadataType: kCMMetadataFormatType_Boxed,
    metadataSpecifications: [spec] as CFArray,
    formatDescriptionOut: &fmtDesc)
guard st == noErr, let fd = fmtDesc else { fail("创建元数据格式失败") }

let mIn = AVAssetWriterInput(mediaType: .metadata, outputSettings: nil, sourceFormatHint: fd)
mIn.expectsMediaDataInRealTime = false
writer.add(mIn)
let adaptor = AVAssetWriterInputMetadataAdaptor(assetWriterInput: mIn)

// movie-level metadata：content identifier
let idItem = AVMutableMetadataItem()
idItem.keySpace = AVMetadataKeySpace.quickTimeMetadata
idItem.key = "com.apple.quicktime.content.identifier" as NSString
idItem.value = identifier as NSString
writer.metadata = [idItem]

guard writer.startWriting() else { fail("writer 启动失败: \(writer.error?.localizedDescription ?? "")") }
writer.startSession(atSourceTime: .zero)
guard reader.startReading() else { fail("reader 启动失败") }

let group = DispatchQueue(label: "lp")
let done = DispatchSemaphore(value: 0)
var wroteMeta = false

vIn.requestMediaDataWhenReady(on: group) {
    while vIn.isReadyForMoreMediaData {
        if let buf = vOut.copyNextSampleBuffer() {
            vIn.append(buf)
        } else {
            vIn.markAsFinished()
            if !wroteMeta {
                wroteMeta = true
                let mi = AVMutableMetadataItem()
                mi.keySpace = AVMetadataKeySpace(rawValue: "mdta")
                mi.key = "com.apple.quicktime.still-image-time" as NSString
                mi.value = 0 as NSNumber
                mi.dataType = kCMMetadataBaseDataType_SInt8 as String
                let g = AVTimedMetadataGroup(
                    items: [mi],
                    timeRange: CMTimeRange(start: .zero,
                                           duration: CMTime(value: 1, timescale: 1)))
                _ = adaptor.append(g)
                mIn.markAsFinished()
                writer.finishWriting { done.signal() }
            }
            break
        }
    }
}
done.wait()
guard writer.status == .completed else { fail("写入失败: \(writer.error?.localizedDescription ?? "")") }

// ---------- 2) 图片写入相同 identifier ----------
let targetImg = outDir.appendingPathComponent(stem + "." + imgURL.pathExtension.lowercased())
if targetImg.path != imgURL.path {
    try? FileManager.default.removeItem(at: targetImg)
    try! FileManager.default.copyItem(at: imgURL, to: targetImg)
}

let exiftool = "/opt/homebrew/bin/exiftool"
func runExif(_ args: [String]) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: exiftool)
    p.arguments = args
    try? p.run()
    p.waitUntilExit()
}

// Apple 的 identifier 写在 MakerNote 里；小红书/微博的图常常没有这块结构，
// 直接写会 "image files unchanged"。先补厂商信息 + 注入 MakerNote 骨架。
let mn = Paths.makerNoteTemplate
if FileManager.default.fileExists(atPath: mn) {
    runExif(["-Make=Apple", "-Model=iPhone SE", "-Software=15.8.8",
             "-overwrite_original", targetImg.path])
    runExif(["-MakerNotes<=" + mn, "-overwrite_original", targetImg.path])
}
runExif(["-Apple:ContentIdentifier=" + identifier, "-overwrite_original", targetImg.path])

print("图片: \(targetImg.path)")
print("视频: \(movURL.path)")
print("标识: \(identifier)")
