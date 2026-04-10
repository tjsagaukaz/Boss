import SwiftUI
import UserNotifications

// MARK: - Main Layout (3-zone: sidebar | centered chat | flexible space)

struct ContentView: View {
    @EnvironmentObject var vm: ChatViewModel
    @State private var showCommandPalette: Bool = false

    var body: some View {
        ZStack {
            mainLayout
                .animation(.easeOut(duration: 0.22), value: vm.pendingPermissionCount > 0)
                .modifier(SurfaceKeyboardShortcuts(vm: vm, showCommandPalette: $showCommandPalette))

            if showCommandPalette {
                CommandPaletteView(isPresented: $showCommandPalette)
                    .transition(.opacity.combined(with: .scale(scale: 0.97, anchor: .top)))
            }
        }
        .animation(.easeOut(duration: 0.15), value: showCommandPalette)
    }

    private var mainLayout: some View {
        ZStack(alignment: .top) {
            HStack(spacing: 0) {
                SidebarView()
                    .frame(width: 240)

                Rectangle()
                    .fill(BossColor.divider)
                    .frame(width: 1)

                ZStack {
                    BossColor.black.ignoresSafeArea()
                    mainSurface
                        .frame(maxWidth: 680)
                }
                .frame(maxWidth: .infinity)
            }
            .background(BossColor.black)
            .preferredColorScheme(.dark)

            if vm.pendingPermissionCount > 0 {
                PermissionBanner()
                    .transition(.move(edge: .top).combined(with: .opacity))
            }

            // Active computer-use session pip (visible from other surfaces)
            if vm.computerState.isActive && vm.selectedSurface != .computer {
                VStack {
                    Spacer()
                    HStack {
                        Spacer()
                        ComputerSessionPip(
                            domain: vm.computerState.session?.currentDomain ?? vm.computerState.session?.targetDomain ?? "session",
                            status: vm.computerState.session?.status ?? .running,
                            onTap: { vm.selectedSurface = .computer }
                        )
                        .padding(.trailing, 20)
                        .padding(.bottom, 16)
                    }
                }
                .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
    }

    @ViewBuilder
    private var mainSurface: some View {
        switch vm.selectedSurface {
        case .chat:
            ChatView()
        case .memory:
            MemoryView()
        case .diagnostics:
            DiagnosticsView()
        case .jobs:
            JobsView()
        case .review:
            ReviewView()
        case .permissions:
            PermissionsView()
        case .preview:
            PreviewView()
        case .workers:
            WorkersView()
        case .deploy:
            UnifiedDeployView()
        case .iosDelivery:
            IOSDeliveryView()
        case .computer:
            ComputerView()
        case .settings:
            SettingsView()
        }
    }
}

// MARK: - Permission Banner

private struct PermissionBanner: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundColor(.orange)
                .font(.system(size: 14, weight: .semibold))

            Text(vm.pendingPermissionCount == 1
                 ? "Approval needed — 1 pending"
                 : "Approval needed — \(vm.pendingPermissionCount) pending")
                .font(.system(size: 13, weight: .medium))
                .foregroundColor(Color.white.opacity(0.9))

            Spacer()

            Button("View") {
                vm.selectedSurface = .chat
            }
            .font(.system(size: 12, weight: .semibold))
            .foregroundColor(.orange)
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color.orange.opacity(0.15))
        .overlay(Rectangle().fill(Color.orange.opacity(0.25)).frame(height: 1), alignment: .bottom)
    }
}

// MARK: - Keyboard Shortcuts

private struct SurfaceKeyboardShortcuts: ViewModifier {
    @ObservedObject var vm: ChatViewModel
    @Binding var showCommandPalette: Bool

    private static let surfaceMap: [Character: AppSurface] = [
        "1": .chat, "2": .memory, "3": .review,
        "4": .deploy, "5": .computer, "6": .settings
    ]

    func body(content: Content) -> some View {
        content
            .onKeyPress(.escape) {
                if showCommandPalette {
                    showCommandPalette = false
                    return .handled
                }
                if vm.selectedSurface != .chat {
                    vm.selectedSurface = .chat
                    return .handled
                }
                return .ignored
            }
            .onKeyPress(characters: CharacterSet(charactersIn: "kK"), phases: .down) { press in
                guard press.modifiers == .command else { return .ignored }
                showCommandPalette.toggle()
                return .handled
            }
            .onKeyPress(characters: CharacterSet(charactersIn: "1234567890"), phases: .down) { press in
                guard press.modifiers == .command,
                      let surface = Self.surfaceMap[press.characters.first ?? " "] else {
                    return .ignored
                }
                vm.selectedSurface = surface
                return .handled
            }
    }
}

// MARK: - Sidebar

struct SidebarView: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button(action: { vm.showChat() }) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Boss")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundColor(BossColor.textPrimary)
                    Spacer()
                }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 16)
            .padding(.top, 20)
            .padding(.bottom, 16)

            if let message = vm.startupIssue {
                InlineStatusBanner(message: message)
                    .padding(.horizontal, 8)
                    .padding(.bottom, 10)
            }

            VStack(spacing: 2) {
                sidebarActionRow("New Chat", icon: "plus") { vm.newSession() }
                sidebarActionRow("Scan System", icon: "arrow.clockwise") { vm.scanSystem() }
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 8)

            sidebarNavRow("Memory", icon: "brain", selected: vm.selectedSurface == .memory) {
                vm.showMemory()
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 2)

            sidebarNavRow("Review", icon: "eye", selected: vm.selectedSurface == .review) {
                vm.showReview()
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 2)

            sidebarNavRow("Deploy", icon: "shippingbox", selected: vm.selectedSurface == .deploy) {
                vm.showDeploy()
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 2)

            sidebarNavRow("Computer", icon: "desktopcomputer", selected: vm.selectedSurface == .computer) {
                vm.selectedSurface = .computer
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 12)

            Rectangle().fill(BossColor.divider).frame(height: 1)
                .padding(.horizontal, 16)

            if let message = vm.sidebarRefreshError {
                InlineStatusBanner(message: message)
                    .padding(.horizontal, 8)
                    .padding(.top, 12)
                    .padding(.bottom, 4)
            }

            ScrollView(.vertical, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 24) {
                    if !vm.savedSessions.isEmpty {
                        sectionHeader("Recent")

                        VStack(spacing: 1) {
                            ForEach(vm.savedSessions, id: \.id) { session in
                                sessionRow(session)
                            }
                        }
                        .padding(.horizontal, 8)
                    }

                    if !vm.projects.isEmpty {
                        sectionHeader("Projects")

                        VStack(spacing: 1) {
                            ForEach(vm.projects) { project in
                                projectRow(project)
                            }
                        }
                        .padding(.horizontal, 8)
                    }
                }
                .padding(.top, 16)
                .padding(.bottom, 20)
            }

            Spacer(minLength: 0)

            // Settings gear button pinned to bottom
            Button(action: { vm.selectedSurface = .settings }) {
                HStack(spacing: 8) {
                    Image(systemName: "gearshape")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundColor(vm.selectedSurface == .settings ? .white : Color.white.opacity(0.45))
                        .frame(width: 16)
                    Text("Settings")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundColor(vm.selectedSurface == .settings ? .white : Color.white.opacity(0.45))
                    Spacer()
                }
                .padding(.vertical, 7)
                .padding(.horizontal, 8)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .fill(vm.selectedSurface == .settings ? Color.white.opacity(0.06) : .clear)
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 8)
            .padding(.bottom, 12)
        }
        .background(BossColor.surface2)
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.system(size: 11, weight: .medium))
            .foregroundColor(Color(hex: "#A1A1AA").opacity(0.45))
            .tracking(1.8)
            .padding(.horizontal, 16)
            .padding(.top, 4)
    }

    private func sidebarActionRow(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(BossColor.textSecondary)
                    .frame(width: 16)
                Text(title)
                    .font(.system(size: 13))
                    .foregroundColor(Color.white.opacity(0.6))
                Spacer()
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func sidebarNavRow(_ title: String, icon: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 0) {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(selected ? BossColor.accent : .clear)
                    .frame(width: 3, height: 16)
                    .padding(.trailing, 7)
                Image(systemName: icon)
                    .font(.system(size: 12, weight: selected ? .semibold : .regular))
                    .foregroundColor(selected ? .white : Color.white.opacity(0.45))
                    .frame(width: 20)
                    .padding(.trailing, 6)
                Text(title)
                    .font(.system(size: 13, weight: selected ? .semibold : .medium))
                    .foregroundColor(selected ? .white : Color.white.opacity(0.50))
                Spacer()
            }
            .padding(.vertical, 7)
            .padding(.horizontal, 8)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(selected ? Color.white.opacity(0.06) : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .animation(.easeInOut(duration: 0.15), value: selected)
    }

    private func sessionRow(_ session: (id: String, title: String, updatedAt: Date)) -> some View {
        let isActive = vm.sessionId == session.id
        return Button {
            vm.loadSession(session.id)
        } label: {
            HStack(spacing: 0) {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(isActive ? BossColor.accent : .clear)
                    .frame(width: 3, height: 14)
                    .padding(.trailing, 7)
                VStack(alignment: .leading, spacing: 2) {
                    Text(session.title)
                        .font(.system(size: 12))
                        .foregroundColor(isActive ? .white : Color.white.opacity(0.55))
                        .lineLimit(1)
                    Text(session.updatedAt, style: .relative)
                        .font(.system(size: 10))
                        .foregroundColor(Color.white.opacity(0.3))
                }
                Spacer()
            }
            .padding(.vertical, 5)
            .padding(.horizontal, 8)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .fill(isActive ? Color.white.opacity(0.06) : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .animation(.easeInOut(duration: 0.15), value: isActive)
        .contextMenu {
            Button(role: .destructive) {
                vm.deleteSession(session.id)
            } label: {
                Label("Delete Session", systemImage: "trash")
            }
        }
    }

    private func projectRow(_ project: ProjectInfo) -> some View {
        let isSelected = vm.selectedSurface == .memory && vm.selectedProjectPath == project.path
        return Button {
            vm.showMemory(projectPath: isSelected ? nil : project.path)
        } label: {
            HStack(spacing: 0) {
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(isSelected ? BossColor.accent : .clear)
                    .frame(width: 3, height: 14)
                    .padding(.trailing, 7)
                Text(project.name)
                    .font(.system(size: 13))
                    .foregroundColor(isSelected ? .white : Color.white.opacity(0.5))
                    .lineLimit(1)
                Spacer()
                if let branch = project.git_branch {
                    Text(branch)
                        .font(.system(size: 10))
                        .foregroundColor(BossColor.textSecondary.opacity(0.5))
                        .lineLimit(1)
                }
            }
            .padding(.vertical, 5)
            .padding(.horizontal, 8)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(isSelected ? Color.white.opacity(0.06) : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .animation(.easeInOut(duration: 0.15), value: isSelected)
    }

    private func memoryPreviewRow(_ item: MemoryRecord) -> some View {
        Button {
            vm.showMemory(projectPath: item.projectPath)
        } label: {
            HStack(spacing: 6) {
                Text(item.label)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.45))
                    .lineLimit(1)

                Text(item.text)
                    .font(.system(size: 11))
                    .foregroundColor(Color.white.opacity(0.3))
                    .lineLimit(1)

                Spacer()
            }
            .padding(.vertical, 3)
            .padding(.horizontal, 8)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color.white.opacity(0.02))
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func factRow(_ fact: FactInfo) -> some View {
        HStack(spacing: 6) {
            Text(fact.key)
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(Color.white.opacity(0.45))
            Text(fact.value)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.3))
                .lineLimit(1)
            Spacer()
        }
        .padding(.vertical, 3)
        .padding(.horizontal, 8)
    }

    private var sidebarMemoryItems: [MemoryRecord] {
        guard let overview = vm.memoryState.memoryOverview else { return [] }
        var seen: Set<String> = []
        var merged: [MemoryRecord] = []
        for section in [overview.preferences, overview.userProfile, overview.recentMemories] {
            for item in section where !seen.contains(item.id) {
                seen.insert(item.id)
                merged.append(item)
            }
        }
        return merged
    }
}

struct InlineStatusBanner: View {
    let message: String

    var body: some View {
        Text(message)
            .font(.system(size: 11))
            .foregroundColor(BossColor.accent.opacity(0.82))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(BossColor.accentSoft)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(BossColor.accent.opacity(0.16), lineWidth: 1)
            )
    }
}

struct SearchBar: View {
    @Binding var text: String
    var placeholder: String = "Search…"

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(Color.white.opacity(0.34))

            TextField(placeholder, text: $text)
                .textFieldStyle(.plain)
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.88))

            if !text.isEmpty {
                Button(action: { text = "" }) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundColor(Color.white.opacity(0.3))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color.white.opacity(0.04))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.white.opacity(0.06), lineWidth: 1)
        )
    }
}

struct CopyButton: View {
    let value: String
    @State private var copied = false

    var body: some View {
        Button(action: {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(value, forType: .string)
            copied = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                copied = false
            }
        }) {
            Image(systemName: copied ? "checkmark" : "doc.on.doc")
                .font(.system(size: 10))
                .foregroundColor(copied ? Color.green.opacity(0.7) : Color.white.opacity(0.3))
                .frame(width: 16, height: 16)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(.plain)
        .help(copied ? "Copied" : "Copy to clipboard")
    }
}
