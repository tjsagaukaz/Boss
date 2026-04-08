import SwiftUI

// MARK: - Main Layout (3-zone: sidebar | centered chat | flexible space)

struct ContentView: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
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
    }

    @ViewBuilder
    private var mainSurface: some View {
        switch vm.selectedSurface {
        case .chat:
            ChatView()
        case .memory:
            MemoryView()
        case .permissions:
            PermissionsView()
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

            sidebarNavRow("Memory", selected: vm.selectedSurface == .memory) {
                vm.showMemory()
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 4)

            sidebarNavRow("Permissions", selected: vm.selectedSurface == .permissions) {
                vm.showPermissions()
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
                VStack(alignment: .leading, spacing: 20) {
                    if !vm.projects.isEmpty {
                        sectionHeader("Projects")

                        VStack(spacing: 1) {
                            ForEach(vm.projects) { project in
                                projectRow(project)
                            }
                        }
                        .padding(.horizontal, 8)
                    }

                    if !sidebarMemoryItems.isEmpty || !vm.facts.isEmpty {
                        sectionHeader("Memory")

                        VStack(spacing: 2) {
                            if !sidebarMemoryItems.isEmpty {
                                ForEach(sidebarMemoryItems.prefix(8)) { item in
                                    memoryPreviewRow(item)
                                }
                            } else {
                                ForEach(vm.facts.prefix(8)) { fact in
                                    factRow(fact)
                                }
                            }
                        }
                        .padding(.horizontal, 8)
                    }
                }
                .padding(.top, 12)
                .padding(.bottom, 20)
            }

            Spacer(minLength: 0)
        }
        .background(BossColor.surface2)
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .foregroundColor(BossColor.textSecondary.opacity(0.4))
            .tracking(1.2)
            .padding(.horizontal, 16)
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

    private func sidebarNavRow(_ title: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack {
                Text(title)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(selected ? .white : Color.white.opacity(0.58))
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
    }

    private func projectRow(_ project: ProjectInfo) -> some View {
        let isSelected = vm.selectedSurface == .memory && vm.selectedProjectPath == project.path
        return Button {
            vm.showMemory(projectPath: isSelected ? nil : project.path)
        } label: {
            HStack(spacing: 8) {
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
        guard let overview = vm.memoryOverview else { return [] }
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
