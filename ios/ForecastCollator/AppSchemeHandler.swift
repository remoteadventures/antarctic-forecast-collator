import Foundation
import WebKit

/// Serves everything the page asks for under afc://app/:
///   /proxy?u=<url>   remote data (Open-Meteo, aviationweather.gov, GitHub), fetched natively with an offline copy
///   /api/obs         METAR/TAF in the shape serve.py returns (latest report per station over the past week)
///   /data/<file>     AMPS exports from the GitHub repo, falling back to the copy bundled in the app
///   anything else    the bundled app/ folder (index.html, locations.json, ...)
/// Responses that came from a saved copy carry an X-AFC-Cached header so the page can say so.
/// A request with "X-AFC-Saved-Only: 1" is answered from saved copies only, without the network, so the
/// page can show the last data instantly on launch and then refresh it.
final class AppSchemeHandler: NSObject, WKURLSchemeHandler {
    static let scheme = "afc"
    static let dataBase = URL(string: "https://raw.githubusercontent.com/remoteadventures/antarctic-forecast-collator/main/app/data/")!
    static let allowedHosts: Set<String> = [
        "api.open-meteo.com", "single-runs-api.open-meteo.com", "aviationweather.gov", "raw.githubusercontent.com",
    ]

    private struct Reply {
        var status = 200
        var type = "application/json"
        var data = Data()
        var savedAt: String? = nil
    }

    private let cache = NetCache()
    private let webRoot = Bundle.main.resourceURL!.appendingPathComponent("app", isDirectory: true).standardizedFileURL
    private var active = Set<ObjectIdentifier>()
    private let lock = NSLock()

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        lock.withLock { _ = active.insert(ObjectIdentifier(task)) }
        guard let url = task.request.url else { return }
        let savedOnly = task.request.value(forHTTPHeaderField: "X-AFC-Saved-Only") == "1"
        Task {
            let reply = await self.reply(for: url, savedOnly: savedOnly)
            await MainActor.run { self.finish(task, url: url, reply) }
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        lock.withLock { _ = active.remove(ObjectIdentifier(task)) }
    }

    @MainActor private func finish(_ task: WKURLSchemeTask, url: URL, _ r: Reply) {
        // WebKit raises if a task is answered after it was stopped (e.g. the page reloaded)
        guard lock.withLock({ active.remove(ObjectIdentifier(task)) != nil }) else { return }
        var headers = ["Content-Type": r.type, "Cache-Control": "no-store"]
        if let s = r.savedAt { headers["X-AFC-Cached"] = s }
        task.didReceive(HTTPURLResponse(url: url, statusCode: r.status, httpVersion: "HTTP/1.1", headerFields: headers)!)
        task.didReceive(r.data)
        task.didFinish()
    }

    private func reply(for url: URL, savedOnly: Bool) async -> Reply {
        let comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let query = Dictionary((comps?.queryItems ?? []).map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { a, _ in a })
        switch url.path {
        case "/proxy":
            guard let target = URL(string: query["u"] ?? ""), let host = target.host,
                  Self.allowedHosts.contains(host) else { return error(403, "host not allowed") }
            return await remote(target, savedOnly: savedOnly)
        case "/api/obs":
            return await observations(metar: query["metar"] ?? "", taf: query["taf"] ?? "", savedOnly: savedOnly)
        case let p where p.hasPrefix("/data/"):
            let name = String(p.dropFirst("/data/".count))
            var r = await remote(Self.dataBase.appendingPathComponent(name), timeout: 15, savedOnly: savedOnly)
            if r.status != 200, let local = bundled("data/" + name) {
                r = local
                r.savedAt = "bundled"
            }
            return r
        default:
            return bundled(String(url.path.dropFirst())) ?? error(404, "not found")
        }
    }

    private func remote(_ url: URL, timeout: TimeInterval = 25, savedOnly: Bool = false) async -> Reply {
        do {
            let r = try await get(url, timeout: timeout, savedOnly: savedOnly)
            return Reply(type: r.contentType, data: r.data, savedAt: r.savedAt.map { ISO8601DateFormatter().string(from: $0) })
        } catch {
            return self.error(502, "offline and no saved copy")
        }
    }

    /// Network fetch with saved-copy fallback, or (savedOnly) the saved copy alone
    private func get(_ url: URL, timeout: TimeInterval = 25, savedOnly: Bool) async throws -> NetCache.Result {
        if savedOnly {
            guard let copy = await cache.saved(url) else { throw URLError(.resourceUnavailable) }
            return copy
        }
        return try await cache.fetch(url, timeout: timeout)
    }

    private func bundled(_ path: String) -> Reply? {
        let file = webRoot.appendingPathComponent(path.isEmpty ? "index.html" : path).standardizedFileURL
        guard file.path.hasPrefix(webRoot.path), let data = try? Data(contentsOf: file) else { return nil }
        let types = ["html": "text/html; charset=utf-8", "json": "application/json", "js": "text/javascript",
                     "css": "text/css", "png": "image/png", "svg": "image/svg+xml"]
        return Reply(type: types[file.pathExtension.lowercased()] ?? "application/octet-stream", data: data)
    }

    private func error(_ status: Int, _ message: String) -> Reply {
        Reply(status: status, data: (try? JSONSerialization.data(withJSONObject: ["error": message])) ?? Data())
    }

    // MARK: METAR / TAF (same logic as serve.py)

    private static let lookbackHours = 168 // many Antarctic stations report intermittently: look back a week
    private static let seriesHours = 30.0  // hours of METARs returned for plotting

    private func observations(metar: String, taf: String, savedOnly: Bool) async -> Reply {
        let valid = { (s: String) in s.range(of: "^[A-Z0-9]{4}(,[A-Z0-9]{4})*$", options: .regularExpression) != nil }
        let metarIds = metar.uppercased(), tafIds = taf.uppercased()
        var out: [String: Any] = ["metar": [Any](), "latest": [String: Any](), "taf": [Any]()]
        var savedAt: Date? = nil
        do {
            if valid(metarIds) {
                let r = try await get(URL(string: "https://aviationweather.gov/api/data/metar?ids=\(metarIds)&format=json&hours=\(Self.lookbackHours)")!, savedOnly: savedOnly)
                savedAt = r.savedAt
                let reports = (try? JSONSerialization.jsonObject(with: r.data)) as? [[String: Any]] ?? []
                let time = { (rec: [String: Any]) in rec["obsTime"] as? Double ?? 0 }
                let cutoff = Date().timeIntervalSince1970 - Self.seriesHours * 3600
                out["metar"] = reports.filter { time($0) >= cutoff }
                var latest: [String: Any] = [:]
                for code in metarIds.split(separator: ",").map(String.init) {
                    latest[code] = reports.filter { $0["icaoId"] as? String == code }.max { time($0) < time($1) } ?? NSNull()
                }
                out["latest"] = latest
            }
            if valid(tafIds) {
                let r = try await get(URL(string: "https://aviationweather.gov/api/data/taf?ids=\(tafIds)&format=json")!, savedOnly: savedOnly)
                if let s = r.savedAt { savedAt = min(savedAt ?? s, s) }
                out["taf"] = (try? JSONSerialization.jsonObject(with: r.data)) as? [Any] ?? []
            }
        } catch {
            return self.error(502, "observations unavailable offline")
        }
        let data = (try? JSONSerialization.data(withJSONObject: out)) ?? Data()
        return Reply(data: data, savedAt: savedAt.map { ISO8601DateFormatter().string(from: $0) })
    }
}
