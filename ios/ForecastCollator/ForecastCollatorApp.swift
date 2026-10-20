import SwiftUI

@main
struct ForecastCollatorApp: App {
    var body: some Scene {
        WindowGroup {
            WebContainer()
                .ignoresSafeArea() // the page pads itself with env(safe-area-inset-*)
        }
    }
}
