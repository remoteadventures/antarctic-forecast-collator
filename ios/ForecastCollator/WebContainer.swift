import SwiftUI
import WebKit

/// Hosts the forecast page (bundled app/ folder) in a WKWebView. Every request the page makes goes
/// through AppSchemeHandler, which supplies the bundled files, fetches remote data natively and keeps
/// an offline copy of each response.
struct WebContainer: UIViewRepresentable {
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(AppSchemeHandler(), forURLScheme: AppSchemeHandler.scheme)
        // Tells the page it is running in the app, so it routes remote requests through the native handler
        config.userContentController.addUserScript(
            WKUserScript(source: "window.AFC_NATIVE = true;", injectionTime: .atDocumentStart, forMainFrameOnly: true))

        let web = WKWebView(frame: .zero, configuration: config)
        // match the page's own background (--page in index.html) so overscroll doesn't flash white
        let page = UIColor { $0.userInterfaceStyle == .dark
            ? UIColor(red: 0.051, green: 0.051, blue: 0.051, alpha: 1)
            : UIColor(red: 0.976, green: 0.976, blue: 0.969, alpha: 1) }
        web.isOpaque = false
        web.backgroundColor = page
        web.scrollView.backgroundColor = page
        web.scrollView.contentInsetAdjustmentBehavior = .never
        web.navigationDelegate = context.coordinator
        #if DEBUG
        web.isInspectable = true // Safari > Develop menu
        #endif

        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.pullToRefresh(_:)), for: .valueChanged)
        web.scrollView.refreshControl = refresh

        context.coordinator.web = web
        web.load(URLRequest(url: URL(string: "\(AppSchemeHandler.scheme)://app/index.html")!))
        return web
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate {
        weak var web: WKWebView?

        @objc func pullToRefresh(_ control: UIRefreshControl) {
            web?.evaluateJavaScript("window.afcRefresh ? window.afcRefresh() : location.reload()")
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { control.endRefreshing() }
        }

        // Links to web sites open in Safari rather than replacing the app's page
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url,
               url.scheme == "http" || url.scheme == "https" {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
        }
    }
}
