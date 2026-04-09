import SwiftUI

struct DeployView: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 24) {
                header
                    .padding(.top, 80)

                if let error = vm.deployRefreshError {
                    InlineStatusBanner(message: error)
                }

                statusCard

                if !vm.deployments.isEmpty {
                    deploymentsSection
                }

                if let deploy = vm.selectedDeployment {
                    deploymentDetail(deploy)
                } else {
                    emptyState
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 32)
        }
        .task {
            await vm.refreshDeploySurface()
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Deploy")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(BossColor.textPrimary)

            Text("Optional preview deployments — requires explicit configuration and approval")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.38))
        }
    }

    // MARK: - Status card

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Deploy Status")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    if let status = vm.deployStatus {
                        Text(status.enabled ? "Configured and ready" : "Not configured — set credentials to enable")
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.34))
                    } else {
                        Text("Loading…")
                            .font(.system(size: 12))
                            .foregroundColor(Color.white.opacity(0.34))
                    }
                }

                Spacer()

                Button(action: { Task { await vm.refreshDeploySurface() } }) {
                    Text("Refresh")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.64))
                }
                .buttonStyle(.plain)
            }

            if let status = vm.deployStatus {
                HStack(spacing: 24) {
                    metric(label: "Adapters", value: status.configuredCount)
                    metric(label: "Live", value: status.liveCount)
                    metric(label: "Recent", value: status.recentDeployments)
                }

                if !status.adapters.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(status.adapters, id: \.adapter) { adapter in
                            HStack(spacing: 6) {
                                Circle()
                                    .fill(adapter.configured ? Color.green.opacity(0.7) : Color.white.opacity(0.2))
                                    .frame(width: 6, height: 6)
                                Text(adapter.adapter)
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundColor(Color.white.opacity(0.7))
                                Text(adapter.configured ? "configured" : "not configured")
                                    .font(.system(size: 11))
                                    .foregroundColor(Color.white.opacity(0.34))
                            }
                        }
                    }
                }
            }
        }
        .padding(16)
        .background(Color.white.opacity(0.04))
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.white.opacity(0.06), lineWidth: 1)
        )
    }

    // MARK: - Deployments list

    private var deploymentsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Deployments")
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.7))

            VStack(spacing: 2) {
                ForEach(vm.deployments) { deploy in
                    Button(action: { vm.selectedDeployment = deploy }) {
                        HStack(spacing: 10) {
                            statusBadge(deploy.status)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(deploy.adapter)
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundColor(Color.white.opacity(0.9))
                                Text(deploy.projectPath.components(separatedBy: "/").suffix(2).joined(separator: "/"))
                                    .font(.system(size: 11))
                                    .foregroundColor(Color.white.opacity(0.4))
                            }
                            Spacer()
                            if let url = deploy.previewUrl {
                                Text(url)
                                    .font(.system(size: 10))
                                    .foregroundColor(Color.blue.opacity(0.7))
                                    .lineLimit(1)
                            }
                        }
                        .padding(.vertical, 6)
                        .padding(.horizontal, 10)
                        .background(
                            deploy.deploymentId == vm.selectedDeployment?.deploymentId
                                ? Color.white.opacity(0.06)
                                : Color.clear
                        )
                        .cornerRadius(6)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: - Detail

    private func deploymentDetail(_ deploy: DeploymentInfo) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Deployment Detail")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.9))
                Spacer()
                statusBadge(deploy.status)
            }

            VStack(alignment: .leading, spacing: 6) {
                metadataLine(label: "ID", value: String(deploy.deploymentId.prefix(12)))
                metadataLine(label: "Adapter", value: deploy.adapter)
                metadataLine(label: "Target", value: deploy.target)
                metadataLine(label: "Project", value: deploy.projectPath)
                if let url = deploy.previewUrl {
                    metadataLine(label: "Preview URL", value: url)
                }
                if let error = deploy.error {
                    metadataLine(label: "Error", value: error)
                }
            }

            if !deploy.buildLog.isEmpty {
                logSection(title: "Build Log", text: deploy.buildLog)
            }
            if !deploy.deployLog.isEmpty {
                logSection(title: "Deploy Log", text: deploy.deployLog)
            }
        }
        .padding(16)
        .background(Color.white.opacity(0.04))
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.white.opacity(0.06), lineWidth: 1)
        )
    }

    // MARK: - Empty state

    private var emptyState: some View {
        VStack(spacing: 8) {
            Text("No deployments yet")
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.5))
            Text("Ask Boss to deploy a project in chat. Requires explicit configuration and approval.")
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.3))
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
    }

    // MARK: - Helpers

    private func metric(label: String, value: Int) -> some View {
        VStack(spacing: 2) {
            Text("\(value)")
                .font(.system(size: 16, weight: .semibold, design: .monospaced))
                .foregroundColor(BossColor.textPrimary)
            Text(label)
                .font(.system(size: 11))
                .foregroundColor(Color.white.opacity(0.34))
        }
    }

    private func metadataLine(label: String, value: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(Color.white.opacity(0.4))
                .frame(width: 80, alignment: .trailing)
            Text(value)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(0.7))
                .textSelection(.enabled)
        }
    }

    private func logSection(title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(Color.white.opacity(0.4))
            ScrollView(.horizontal, showsIndicators: true) {
                Text(text.suffix(2000))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(Color.white.opacity(0.5))
                    .textSelection(.enabled)
            }
            .frame(maxHeight: 120)
        }
    }

    private func statusBadge(_ status: String) -> some View {
        let color: Color = {
            switch status {
            case "live": return .green
            case "building", "deploying": return .blue
            case "failed": return .red
            case "cancelled", "torn_down": return .gray
            default: return .yellow
            }
        }()
        return Text(status)
            .font(.system(size: 10, weight: .medium))
            .foregroundColor(color.opacity(0.9))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.15))
            .cornerRadius(4)
    }
}
