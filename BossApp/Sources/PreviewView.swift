import SwiftUI

struct PreviewView: View {
    @EnvironmentObject var vm: ChatViewModel

    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 24) {
                header
                    .padding(.top, 80)

                if let message = vm.previewRefreshError {
                    InlineStatusBanner(message: message)
                }

                if let status = vm.previewStatus {
                    capabilitiesCard(status.capabilities, visionAvailable: status.visionAvailable ?? false)
                    sessionsCard(status.sessions, activeCount: status.activeCount)
                } else {
                    emptyState
                }
            }
            .frame(maxWidth: 680, alignment: .leading)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.bottom, 32)
        }
        .task {
            await vm.refreshPreviewSurface()
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Preview")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(BossColor.textPrimary)

                Text("Local preview server lifecycle and screenshot capture status")
                    .font(.system(size: 13))
                    .foregroundColor(Color.white.opacity(0.38))
            }

            Spacer()

            Button(action: { Task { await vm.refreshPreviewSurface() } }) {
                Text("Refresh")
                    .font(.system(size: 12))
                    .foregroundColor(Color.white.opacity(0.64))
            }
            .buttonStyle(.plain)
        }
    }

    private func capabilitiesCard(_ caps: PreviewCapabilitiesInfo, visionAvailable: Bool) -> some View {
        card {
            VStack(alignment: .leading, spacing: 12) {
                Text("Capabilities")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.9))

                HStack(spacing: 24) {
                    capabilityDot("Browser", available: caps.hasBrowser)
                    capabilityDot("Playwright", available: caps.hasPlaywright)
                    capabilityDot("Node.js", available: caps.hasNode)
                    capabilityDot("Swift Build", available: caps.hasSwiftBuild)
                }

                HStack(spacing: 24) {
                    capabilityDot("Vision Input", available: visionAvailable)
                    capabilityDot("Policy Enforced", available: caps.policyEnforced ?? false)
                }

                if let path = caps.browserPath {
                    metadataLine(label: "Browser Path", value: path)
                }
            }
        }
    }

    private func sessionsCard(_ sessions: [PreviewSessionInfo], activeCount: Int) -> some View {
        card {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("Sessions")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundColor(Color.white.opacity(0.9))

                    statusBadge(activeCount > 0 ? "\(activeCount) active" : "None")
                }

                if sessions.isEmpty {
                    Text("No preview sessions. Start one from chat or the API.")
                        .font(.system(size: 12))
                        .foregroundColor(Color.white.opacity(0.38))
                } else {
                    ForEach(sessions, id: \.sessionId) { session in
                        sessionRow(session)
                    }
                }
            }
        }
    }

    private func sessionRow(_ session: PreviewSessionInfo) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Rectangle().fill(Color.white.opacity(0.05)).frame(height: 1)

            HStack(spacing: 16) {
                metric(label: "Status", value: session.status.capitalized)
                if let url = session.url {
                    metric(label: "URL", value: url)
                }
                if let pid = session.pid {
                    metric(label: "PID", value: "\(pid)")
                }
            }

            HStack(spacing: 16) {
                if let method = session.verificationMethod {
                    verificationBadge(method)
                }
                if session.policyEnforced == true {
                    statusBadge("Policy Enforced")
                }
            }

            metadataLine(label: "Project", value: session.projectPath)

            if let cmd = session.startCommand {
                metadataLine(label: "Command", value: cmd)
            }

            if let err = session.errorMessage {
                Text(err)
                    .font(.system(size: 11))
                    .foregroundColor(Color.red.opacity(0.8))
            }

            if let capture = session.lastCapture {
                captureSection(capture)
            }
        }
    }

    private func captureSection(_ capture: PreviewCaptureInfo) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 12) {
                if let method = capture.verificationMethod {
                    verificationBadge(method)
                }
                if let detail = capture.detailMode {
                    statusBadge("Detail: \(detail)")
                }
            }
            if let title = capture.pageTitle {
                metadataLine(label: "Page Title", value: title)
            }
            if let path = capture.screenshotPath {
                metadataLine(label: "Screenshot", value: path)
            }
            if let errors = capture.consoleErrors, !errors.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("CONSOLE ERRORS")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(Color.white.opacity(0.28))
                        .tracking(1.0)
                    ForEach(errors, id: \.self) { err in
                        Text(err)
                            .font(.system(size: 11))
                            .foregroundColor(Color.red.opacity(0.7))
                    }
                }
            }
            if let errors = capture.networkErrors, !errors.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("NETWORK ERRORS")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundColor(Color.white.opacity(0.28))
                        .tracking(1.0)
                    ForEach(errors, id: \.self) { err in
                        Text(err)
                            .font(.system(size: 11))
                            .foregroundColor(Color.orange.opacity(0.7))
                    }
                }
            }
        }
    }

    // MARK: - Primitives

    private func verificationBadge(_ method: String) -> some View {
        let color: Color = {
            switch method {
            case "visual": return Color.green.opacity(0.7)
            case "textual": return Color.yellow.opacity(0.7)
            default: return Color.white.opacity(0.3)
            }
        }()
        return Text(method.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .foregroundColor(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(
                Capsule()
                    .fill(color.opacity(0.15))
            )
    }

    private func capabilityDot(_ label: String, available: Bool) -> some View {
        HStack(spacing: 5) {
            Circle()
                .fill(available ? Color.green.opacity(0.7) : Color.white.opacity(0.15))
                .frame(width: 7, height: 7)
            Text(label)
                .font(.system(size: 12))
                .foregroundColor(Color.white.opacity(available ? 0.72 : 0.32))
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
            Text("Loading preview status")
                .font(.system(size: 14, weight: .medium))
                .foregroundColor(Color.white.opacity(0.82))

            Text("Boss is checking preview capabilities and active sessions.")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.34))
        }
    }
}
