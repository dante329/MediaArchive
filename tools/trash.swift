// 把文件/文件夹移到废纸篓（系统 API，可恢复）
// 用法： ma-trash <路径>...
import Foundation

let args = CommandLine.arguments.dropFirst()
if args.isEmpty {
    FileHandle.standardError.write("用法: ma-trash <路径>...\n".data(using: .utf8)!)
    exit(2)
}
var failed = 0
for p in args {
    var url = URL(fileURLWithPath: p)
    do {
        try FileManager.default.trashItem(at: url, resultingItemURL: nil)
        print("trashed: \(p)")
    } catch {
        FileHandle.standardError.write("failed: \(p) — \(error.localizedDescription)\n".data(using: .utf8)!)
        failed += 1
    }
}
exit(failed == 0 ? 0 : 1)
