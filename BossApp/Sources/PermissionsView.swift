import SwiftUI

struct PermissionsView: View {
    @EnvironmentObject var vm: ChatViewModel

    private let sectionOrder = ["Applications", "System", "Web", "Memory", "Other"]

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                header
                    .padding(.top, 80)
                    .padding(.bottom, 28)

                if let message = vm.permissionsRefreshError {
                    InlineStatusBanner(message: message)
                        .padding(.bottom, 20)
                }

                if vm.permissions.isEmpty {
                    emptyState
                } else {
                    permissionSections
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 32)
        }
        .task {
            await vm.refreshPermissions()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Permissions")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(BossColor.textPrimary)

            Text("Control what Boss is allowed to do")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.38))
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("No stored permissions")
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.82))

            Text("Boss will ask before performing sensitive actions.")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.34))
        }
        .padding(.top, 8)
    }

    private var permissionSections: some View {
        let grouped = Dictionary(grouping: vm.permissions, by: permissionGroup)
        let ordered = sectionOrder.filter { grouped[$0] != nil }
        let extra = grouped.keys.filter { !sectionOrder.contains($0) }.sorted()

        return VStack(alignment: .leading, spacing: 24) {
            ForEach(ordered + extra, id: \.self) { section in
                if let entries = grouped[section], !entries.isEmpty {
                    PermissionSectionView(
                        title: section,
                        entries: entries,
                        onRevoke: { vm.revokePermission($0) }
                    )
                }
            }
        }
    }

    private func permissionGroup(for entry: PermissionEntry) -> String {
        switch entry.tool {
        case "open_app":
            return "Applications"
        case "web_search":
            return "Web"
        case "remember":
            return "Memory"
        default:
            return "System"
        }
    }
}

private struct PermissionSectionView: View {
    let title: String
    let entries: [PermissionEntry]
    let onRevoke: (PermissionEntry) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor(Color.white.opacity(0.34))
                .tracking(1.2)
                .padding(.bottom, 10)

            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                    PermissionRowView(entry: entry, onRevoke: { onRevoke(entry) })

                    if index < entries.count - 1 {
                        Rectangle()
                            .fill(Color.white.opacity(0.05))
                            .frame(height: 1)
                    }
                }
            }
        }
    }
}

private struct PermissionRowView: View {
    let entry: PermissionEntry
    let onRevoke: () -> Void

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(entry.rowTitle)
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.9))

            Text(entry.decision.label)
                .font(.system(size: 13))
                .foregroundColor(entry.decision == .allow ? Color.white.opacity(0.65) : Color.white.opacity(0.4))

            HStack {
                Text(lastUsedText)
                    .font(.system(size: 12))
                    .foregroundColor(Color.white.opacity(0.32))

                Spacer()

                Button(action: onRevoke) {
                    Text("Revoke")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.6))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.vertical, 12)
    }

    private var lastUsedText: String {
        if let date = entry.lastUsedAt {
            let relative = Self.relativeFormatter.localizedString(for: date, relativeTo: Date())
            return "Last used \(relative)"
        }
        if let updated = entry.updatedAt {
            let relative = Self.relativeFormatter.localizedString(for: updated, relativeTo: Date())
            return "Updated \(relative)"
        }
        return "Not used yet"
    }
}