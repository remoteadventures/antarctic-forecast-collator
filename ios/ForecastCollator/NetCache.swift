import Foundation
import CryptoKit

/// Fetches a URL and keeps the last good response on disk. When the network fails (common on the ice),
/// the saved copy is returned instead, with the time it was saved.
actor NetCache {
    struct Result {
        let data: Data
        let contentType: String
        /// nil when fresh from the network; otherwise when the saved copy was fetched
        let savedAt: Date?
    }

    private let dir: URL

    init() {
        dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("afc-net", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    private func files(_ url: URL) -> (body: URL, meta: URL) {
        let key = SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
        return (dir.appendingPathComponent(key), dir.appendingPathComponent(key + ".json"))
    }

    /// The saved copy only (no network), or nil if there isn't one
    func saved(_ url: URL) -> Result? {
        let (body, meta) = files(url)
        guard let data = try? Data(contentsOf: body),
              let m = (try? JSONSerialization.jsonObject(with: Data(contentsOf: meta))) as? [String: Any] else { return nil }
        return Result(data: data, contentType: m["type"] as? String ?? "application/json",
                      savedAt: Date(timeIntervalSince1970: m["at"] as? Double ?? 0))
    }

    func fetch(_ url: URL, timeout: TimeInterval = 25) async throws -> Result {
        let (body, meta) = files(url)
        var req = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.setValue("antarctic-forecast-collator-ios/0.1", forHTTPHeaderField: "User-Agent")
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            let type = http.value(forHTTPHeaderField: "Content-Type") ?? "application/octet-stream"
            try? data.write(to: body, options: .atomic)
            if let m = try? JSONSerialization.data(withJSONObject: ["type": type, "at": Date().timeIntervalSince1970]) {
                try? m.write(to: meta, options: .atomic)
            }
            return Result(data: data, contentType: type, savedAt: nil)
        } catch {
            guard let copy = saved(url) else { throw error }
            return copy
        }
    }
}
