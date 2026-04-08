import SwiftUI

struct MemoryView: View {
    @EnvironmentObject var vm: ChatViewModel
    @State private var expandedProjectIDs: Set<String> = []

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                header
                    .padding(.top, 80)
                    .padding(.bottom, 28)

                if let message = vm.memoryRefreshError {
                    InlineStatusBanner(message: message)
                        .padding(.bottom, 20)
                }

                if let overview = vm.memoryOverview {
                    VStack(alignment: .leading, spacing: 24) {
                        scanStatusCard(overview.scanStatus)

                        if let currentTurn = overview.currentTurnMemory {
                            currentTurnSection(currentTurn)
                        }

                        if !overview.userProfile.isEmpty {
                            memorySection(
                                title: "User Profile",
                                subtitle: "Durable facts Boss keeps about you",
                                items: overview.userProfile
                            )
                        }

                        if !overview.preferences.isEmpty {
                            memorySection(
                                title: "Preferences",
                                subtitle: "Stable choices and defaults",
                                items: overview.preferences
                            )
                        }

                        if !overview.recentMemories.isEmpty {
                            memorySection(
                                title: "Recent Memories",
                                subtitle: "Most recently confirmed or used memories",
                                items: overview.recentMemories.prefix(10).map { $0 }
                            )
                        }

                        if !overview.conversationSummaries.isEmpty {
                            memorySection(
                                title: "Conversation Summaries",
                                subtitle: "Condensed session context",
                                items: overview.conversationSummaries
                            )
                        }

                        if !orderedProjectSummaries(overview).isEmpty {
                            projectSummariesSection(orderedProjectSummaries(overview))
                        }
                    }
                } else {
                    emptyState
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 32)
        }
        .task {
            await vm.refreshMemoryOverview()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Memory")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(BossColor.textPrimary)

            Text("Inspect what Boss knows, what was injected, and what was scanned")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.38))
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Loading memory")
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.82))

            Text("Boss is fetching durable memory, summaries, and project context.")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.34))
        }
    }

    private func scanStatusCard(_ scanStatus: ScanStatusInfo) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Scan Status")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    Text(scanStatus.lastScanAt.map { "Last scan \(relativeDate($0))" } ?? "No project scan recorded yet")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.34))
                }

                Spacer()

                Button(action: { vm.scanSystem() }) {
                    Text("Rescan")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.64))
                }
                .buttonStyle(.plain)
            }

            HStack(spacing: 24) {
                scanMetric("Projects", value: scanStatus.projectsIndexed)
                scanMetric("Files", value: scanStatus.filesIndexed)
                scanMetric("Summaries", value: scanStatus.projectNotes)
                scanMetric("Chunks", value: scanStatus.fileChunks)
            }
        }
        .padding(16)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color.white.opacity(0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.05), lineWidth: 1)
        )
    }

    private func scanMetric(_ label: String, value: Int) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)")
                .font(.system(size: 15, weight: .medium))
                .foregroundColor(Color.white.opacity(0.88))
            Text(label)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.32))
        }
    }

    private func currentTurnSection(_ injection: MemoryInjectionInfo) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            sectionTitle("Current Turn", subtitle: "Why memory matched the active or latest turn")
                .padding(.bottom, 10)

            VStack(alignment: .leading, spacing: 10) {
                Text(injection.message)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.82))

                if let projectPath = injection.projectPath {
                    Text("Project scope: \(projectPath)")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.36))
                }

                if !injection.reasons.isEmpty {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(injection.reasons.enumerated()), id: \.element.id) { index, reason in
                            memoryReasonRow(reason)

                            if index < injection.reasons.count - 1 {
                                Rectangle()
                                    .fill(Color.white.opacity(0.05))
                                    .frame(height: 1)
                            }
                        }
                    }
                } else {
                    Text("No persisted memories were relevant for this turn.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.34))
                }
            }
        }
    }

    private func memoryReasonRow(_ reason: MemoryInjectionReason) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(reason.key)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.88))

                Spacer()

                if reason.deletable {
                    Button(action: { vm.forgetMemory(sourceTable: reason.sourceTable, itemId: reason.memoryId) }) {
                        Text("Forget")
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.54))
                    }
                    .buttonStyle(.plain)
                }
            }

            Text(reason.text)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.58))

            Text(reason.why)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.32))
        }
        .padding(.vertical, 12)
    }

    private func memorySection(title: String, subtitle: String, items: [MemoryRecord]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            sectionTitle(title, subtitle: subtitle)
                .padding(.bottom, 10)

            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    memoryRow(item)

                    if index < items.count - 1 {
                        Rectangle()
                            .fill(Color.white.opacity(0.05))
                            .frame(height: 1)
                    }
                }
            }
        }
    }

    private func memoryRow(_ item: MemoryRecord) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(item.label)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.9))

                Spacer()

                if item.deletable {
                    Button(action: { vm.forgetMemory(sourceTable: item.sourceTable, itemId: item.memoryId) }) {
                        Text("Forget")
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.54))
                    }
                    .buttonStyle(.plain)
                }
            }

            Text(item.text)
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.58))

            HStack(spacing: 10) {
                Text(item.memoryKind.replacingOccurrences(of: "_", with: " ").capitalized)
                if let projectPath = item.projectPath {
                    Text(projectPath)
                }
                if let updatedAt = item.updatedAt {
                    Text("Updated \(relativeDate(updatedAt))")
                }
            }
            .font(.system(size: 11))
            .foregroundColor(Color.white.opacity(0.3))
        }
        .padding(.vertical, 12)
    }

    private func projectSummariesSection(_ projects: [ProjectSummaryInfo]) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            sectionTitle("Project Summaries", subtitle: "Scanned repo context and entry points")
                .padding(.bottom, 10)

            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(projects.enumerated()), id: \.element.id) { index, project in
                    projectSummaryRow(project)

                    if index < projects.count - 1 {
                        Rectangle()
                            .fill(Color.white.opacity(0.05))
                            .frame(height: 1)
                    }
                }
            }
        }
    }

    private func projectSummaryRow(_ project: ProjectSummaryInfo) -> some View {
        let isExpanded = expandedProjectIDs.contains(project.id) || vm.selectedProjectPath == project.projectPath

        return VStack(alignment: .leading, spacing: 8) {
            Button {
                if expandedProjectIDs.contains(project.id) {
                    expandedProjectIDs.remove(project.id)
                } else {
                    expandedProjectIDs.insert(project.id)
                }
                vm.selectedProjectPath = project.projectPath
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(project.projectName)
                            .font(.system(size: 14, weight: .medium))
                            .foregroundColor(Color.white.opacity(0.9))

                        Text("\(project.projectType) · \(project.projectPath)")
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.34))
                            .lineLimit(1)
                    }

                    Spacer()

                    if let branch = project.gitBranch {
                        Text(branch)
                            .font(.system(size: 11))
                            .foregroundColor(Color.white.opacity(0.34))
                    }

                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.4))
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded {
                VStack(alignment: .leading, spacing: 10) {
                    Text(project.summaryText)
                        .font(.system(size: 13))
                        .foregroundColor(Color.white.opacity(0.6))
                        .textSelection(.enabled)

                    metadataList(title: "Stack", values: stringList(from: project.metadata?["stack"]?.value))
                    metadataList(title: "Entry Points", values: stringList(from: project.metadata?["entry_points"]?.value))
                    metadataList(title: "Useful Commands", values: stringList(from: project.metadata?["useful_commands"]?.value))
                    metadataList(title: "Notable Modules", values: stringList(from: project.metadata?["notable_modules"]?.value))

                    HStack {
                        if let lastScanned = project.lastScanned {
                            Text("Scanned \(relativeDate(lastScanned))")
                                .font(.system(size: 11))
                                .foregroundColor(Color.white.opacity(0.3))
                        }

                        Spacer()

                        if project.deletable {
                            Button(action: { vm.forgetMemory(sourceTable: project.sourceTable, itemId: project.memoryId) }) {
                                Text("Forget")
                                    .font(.system(size: 12))
                                    .foregroundColor(Color.white.opacity(0.54))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                .padding(.top, 4)
            }
        }
        .padding(.vertical, 12)
    }

    private func metadataList(title: String, values: [String]) -> some View {
        Group {
            if !values.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title.uppercased())
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(Color.white.opacity(0.28))
                        .tracking(1.1)

                    ForEach(values.prefix(6), id: \.self) { value in
                        Text(value)
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.5))
                    }
                }
            }
        }
    }

    private func sectionTitle(_ title: String, subtitle: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.86))

            Text(subtitle)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.32))
        }
    }

    private func orderedProjectSummaries(_ overview: MemoryOverview) -> [ProjectSummaryInfo] {
        guard let selected = vm.selectedProjectPath else {
            return overview.projectSummaries
        }
        return overview.projectSummaries.sorted { lhs, rhs in
            if lhs.projectPath == selected { return true }
            if rhs.projectPath == selected { return false }
            return lhs.projectName.localizedCaseInsensitiveCompare(rhs.projectName) == .orderedAscending
        }
    }

    private func stringList(from value: Any?) -> [String] {
        if let strings = value as? [String] {
            return strings
        }
        if let codables = value as? [AnyCodable] {
            return codables.compactMap { $0.value as? String }
        }
        return []
    }

    private func relativeDate(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: Date())
    }
}