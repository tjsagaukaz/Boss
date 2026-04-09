import SwiftUI

struct DiagnosticsView: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 24) {
                header
                    .padding(.top, 80)

                if let message = vm.diagnosticsRefreshError {
                    InlineStatusBanner(message: message)
                }

                if let status = vm.systemStatus {
                    overviewCard(status)
                    repoCard(status)
                    runtimeCard(status)
                    bossControlCard(status)
                    providersCard(status)
                    deployCard
                    promptLayersCard
                } else {
                    emptyState
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 32)
        }
        .task {
            await vm.refreshDiagnosticsSurface()
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Diagnostics")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(BossColor.textPrimary)

                Text("High-signal local health for the repo, runtime, and Boss control files")
                    .font(.system(size: 13))
                    .foregroundColor(Color.white.opacity(0.38))
            }

            Spacer()

            Button(action: { Task { await vm.refreshDiagnosticsSurface() } }) {
                Text("Refresh")
                    .font(.system(size: 12))
                    .foregroundColor(Color.white.opacity(0.64))
            }
            .buttonStyle(.plain)
        }
    }

    private func overviewCard(_ status: SystemStatusInfo) -> some View {
        let diagnostics = status.diagnostics
        return card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Overview")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.9))

                HStack(spacing: 24) {
                    metric(label: "Active Mode", value: vm.selectedMode.label)
                    metric(label: "Provider", value: status.providerMode ?? diagnostics?.providerMode ?? "unknown")
                    metric(label: "Pending Memory", value: "\(diagnostics?.pendingMemoryCount ?? 0)")
                    metric(label: "Pending Jobs", value: "\(diagnostics?.pendingJobsCount ?? status.backgroundJobsCount ?? 0)")
                    metric(label: "Pending Runs", value: "\(diagnostics?.pendingRunsCount ?? status.pendingRunsCount ?? 0)")
                }
            }
        }
    }

    private func repoCard(_ status: SystemStatusInfo) -> some View {
        let git = status.git
        let diagnostics = status.diagnostics
        return card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Repository")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    statusBadge((diagnostics?.repoClean ?? git?.clean) == true ? "Clean" : "Needs attention")
                }

                Text(git?.summary ?? diagnostics?.gitSummary ?? "Git summary unavailable.")
                    .font(.system(size: 12))
                    .foregroundColor(Color.white.opacity(0.58))

                if let repoRoot = git?.repoRoot {
                    metadataLine(label: "Repo Root", value: repoRoot)
                }
                if let branch = git?.branch ?? git?.branchSummary {
                    metadataLine(label: "Branch", value: branch)
                }
            }
        }
    }

    private func runtimeCard(_ status: SystemStatusInfo) -> some View {
        let diagnostics = status.diagnostics
        let warnings = diagnostics?.statusWarnings ?? status.runtimeTrust?.warnings ?? []
        let consistent = diagnostics?.lockConsistent ?? false

        return card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Runtime")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    statusBadge(consistent ? "Consistent" : "Check lock")
                }

                if let workspacePath = status.workspacePath {
                    metadataLine(label: "Workspace", value: workspacePath)
                }
                if let interpreterPath = status.interpreterPath {
                    metadataLine(label: "Interpreter", value: interpreterPath)
                }
                if let readyAt = status.readyAt {
                    metadataLine(label: "Ready", value: relativeDate(readyAt))
                }
                if let buildMarker = status.buildMarker {
                    metadataLine(label: "Build", value: buildMarker)
                }

                if warnings.isEmpty {
                    Text("No runtime trust warnings.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.42))
                } else {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("WARNINGS")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundColor(Color.white.opacity(0.28))
                            .tracking(1.0)
                        ForEach(warnings, id: \.self) { warning in
                            Text(warning)
                                .font(.system(size: 12))
                                .foregroundColor(Color.white.opacity(0.58))
                        }
                    }
                }
            }
        }
    }

    private func bossControlCard(_ status: SystemStatusInfo) -> some View {
        let health = status.bossControlHealth
        let files = status.bossControl?.files ?? [:]
        return card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Boss Control")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    statusBadge((health?.healthy ?? false) ? "Healthy" : "Incomplete")
                }

                metadataLine(label: "Default Mode", value: health?.defaultMode ?? status.bossControl?.defaultMode ?? "unknown")
                metadataLine(label: "Review Mode", value: health?.reviewModeName ?? status.bossControl?.reviewModeName ?? "review")
                metadataLine(label: "Rules", value: "\(health?.rulesCount ?? status.bossControl?.rules?.count ?? 0)")

                controlFileRow(label: "BOSS.md", file: files["BOSS.md"])
                controlFileRow(label: ".boss/config.toml", file: files["config"])

                if let missing = health?.missingFiles, !missing.isEmpty {
                    metadataLine(label: "Missing", value: missing.joined(separator: ", "))
                }
            }
        }
    }

    private func controlFileRow(label: String, file: BossControlFileStatusInfo?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(label)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(Color.white.opacity(0.82))

            statusBadge((file?.exists ?? false) ? "Present" : "Missing")

            Spacer()

            if let path = file?.path {
                Text(path)
                    .font(.system(size: 11))
                    .foregroundColor(Color.white.opacity(0.34))
                    .lineLimit(1)
            }
        }
    }

    private func providersCard(_ status: SystemStatusInfo) -> some View {
        let registry = status.providerRegistry
        return card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Providers")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.9))

                if let providers = registry?.providers, !providers.isEmpty {
                    ForEach(providers) { provider in
                        providerRow(provider)
                    }

                    if let routing = registry?.routing, !routing.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("ROUTING")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundColor(Color.white.opacity(0.28))
                                .tracking(1.0)
                            ForEach(routing.sorted(by: { $0.key < $1.key }), id: \.key) { mode, target in
                                HStack(spacing: 6) {
                                    Text(mode)
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundColor(Color.white.opacity(0.64))
                                        .frame(width: 80, alignment: .leading)
                                    Text("→")
                                        .font(.system(size: 11))
                                        .foregroundColor(Color.white.opacity(0.32))
                                    Text(target)
                                        .font(.system(size: 11))
                                        .foregroundColor(Color.white.opacity(0.58))
                                }
                            }
                        }
                    }
                } else {
                    Text("No providers registered.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.42))
                }
            }
        }
    }

    private func providerRow(_ provider: ProviderInfoItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(provider.name)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(Color.white.opacity(0.82))

                Text(provider.kind)
                    .font(.system(size: 10))
                    .foregroundColor(Color.white.opacity(0.38))

                Spacer()

                providerHealthBadge(provider.health)
            }

            if let caps = provider.capabilities, !caps.isEmpty {
                HStack(spacing: 8) {
                    ForEach(caps, id: \.self) { cap in
                        HStack(spacing: 3) {
                            Circle()
                                .fill(Color.green.opacity(0.7))
                                .frame(width: 5, height: 5)
                            Text(cap)
                                .font(.system(size: 10))
                                .foregroundColor(Color.white.opacity(0.58))
                        }
                    }
                }
            }
        }
        .padding(.vertical, 2)
    }

    private func providerHealthBadge(_ health: ProviderHealthInfo?) -> some View {
        let status = health?.status ?? "unchecked"
        let color: Color = {
            switch status {
            case "healthy": return .green
            case "degraded": return .yellow
            case "unavailable": return .red
            default: return Color.white.opacity(0.3)
            }
        }()
        return HStack(spacing: 4) {
            Circle()
                .fill(color.opacity(0.8))
                .frame(width: 6, height: 6)
            Text(status.capitalized)
                .font(.system(size: 10))
                .foregroundColor(color.opacity(0.9))
            if let ms = health?.latencyMs {
                Text(String(format: "%.0fms", ms))
                    .font(.system(size: 9))
                    .foregroundColor(Color.white.opacity(0.28))
            }
        }
    }

    private var deployCard: some View {
        card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Deploy")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    if let ds = vm.deployStatus {
                        statusBadge(ds.enabled ? "Enabled" : "Disabled")
                    }
                }

                if let ds = vm.deployStatus {
                    HStack(spacing: 24) {
                        metric(label: "Configured", value: "\(ds.configuredCount)")
                        metric(label: "Live Deploys", value: "\(ds.liveCount)")
                    }

                    if !ds.adapters.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("ADAPTERS")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundColor(Color.white.opacity(0.28))
                                .tracking(1.0)
                            ForEach(ds.adapters, id: \.adapter) { info in
                                HStack(spacing: 6) {
                                    Circle()
                                        .fill(info.configured ? Color.green.opacity(0.7) : Color.white.opacity(0.15))
                                        .frame(width: 6, height: 6)
                                    Text(info.adapter)
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundColor(Color.white.opacity(info.configured ? 0.78 : 0.38))
                                    Spacer()
                                    Text(info.configured ? "Ready" : "Not configured")
                                        .font(.system(size: 10))
                                        .foregroundColor(Color.white.opacity(0.34))
                                }
                            }
                        }
                    }
                } else {
                    Text("Deploy status not loaded.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.42))
                }
            }
        }
    }

    private var promptLayersCard: some View {
        let diag = vm.promptDiagnostics
        return card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Prompt Layers")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    if let diag {
                        statusBadge("\(diag.activeLayers)/\(diag.totalLayers) active")
                    }
                }

                if let diag {
                    HStack(spacing: 24) {
                        metric(label: "Mode", value: diag.mode)
                        metric(label: "Agent", value: diag.agentName)
                        metric(label: "Chars", value: "\(diag.totalChars)")
                    }

                    HStack(spacing: 16) {
                        flagBadge("Review", active: diag.reviewGuidanceActive ?? false)
                        flagBadge("Frontend", active: diag.frontendGuidanceActive ?? false)
                    }

                    if let kinds = diag.activeKinds, !kinds.isEmpty {
                        metadataLine(label: "Active Kinds", value: kinds.joined(separator: ", "))
                    }

                    if let sources = diag.instructionSources, !sources.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("INSTRUCTION SOURCES")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundColor(Color.white.opacity(0.28))
                                .tracking(1.0)
                            ForEach(sources, id: \.self) { source in
                                Text(source)
                                    .font(.system(size: 11))
                                    .foregroundColor(Color.white.opacity(0.52))
                                    .lineLimit(1)
                            }
                        }
                    }

                    if let layers = diag.layers {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("LAYERS")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundColor(Color.white.opacity(0.28))
                                .tracking(1.0)
                            ForEach(Array(layers.enumerated()), id: \.offset) { _, layer in
                                HStack(spacing: 8) {
                                    Circle()
                                        .fill(layer.active ? Color.green.opacity(0.7) : Color.white.opacity(0.15))
                                        .frame(width: 6, height: 6)
                                    Text(layer.kind)
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundColor(Color.white.opacity(layer.active ? 0.78 : 0.32))
                                    Spacer()
                                    Text("\(layer.contentLength) chars")
                                        .font(.system(size: 10))
                                        .foregroundColor(Color.white.opacity(0.28))
                                }
                            }
                        }
                    }
                } else {
                    Text("Prompt diagnostics not loaded.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.42))
                }
            }
        }
    }

    private func flagBadge(_ label: String, active: Bool) -> some View {
        HStack(spacing: 4) {
            Circle()
                .fill(active ? Color.green.opacity(0.7) : Color.white.opacity(0.15))
                .frame(width: 6, height: 6)
            Text(label)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(active ? 0.78 : 0.38))
        }
    }

    private func metric(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(size: 15, weight: .medium))
                .foregroundColor(Color.white.opacity(0.88))
            Text(label)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.32))
        }
    }

    private func metadataLine(label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label.uppercased())
                .font(.system(size: 10, weight: .semibold))
                .foregroundColor(Color.white.opacity(0.28))
                .tracking(1.0)
            Text(value)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.58))
                .textSelection(.enabled)
        }
    }

    private func statusBadge(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .foregroundColor(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(
                Capsule()
                    .fill(Color.white.opacity(0.18))
            )
    }

    private func card<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
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

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Loading diagnostics")
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.82))

            Text("Boss is fetching runtime, git, and control health information.")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.34))
        }
    }

    private func relativeDate(_ date: Date) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter.localizedString(for: date, relativeTo: Date())
    }
}