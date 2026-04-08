import SwiftUI
import AppKit

@main
struct BossApp: App {
    @StateObject private var chatVM = ChatViewModel()

    init() {
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(chatVM)
                .frame(minWidth: 900, minHeight: 600)
                .background(Color(hex: "#000000"))
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1200, height: 800)
    }
}
