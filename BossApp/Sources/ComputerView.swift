import SwiftUI

// MARK: - Coordinate Mapping

/// Maps absolute viewport coordinates (from the backend browser) to local
/// SwiftUI view coordinates, accounting for aspect-fit letterboxing.
struct CoordinateMapper {
    /// The viewport size used by the backend browser (e.g. 1280×800).
    let viewport: CGSize
    /// The frame of the container view (where the image is drawn aspect-fit).
    let containerFrame: CGSize

    /// The rendered image rect within the container, after aspect-fit.
    var renderedRect: CGRect {
        guard viewport.width > 0, viewport.height > 0,
              containerFrame.width > 0, containerFrame.height > 0 else {
            return .zero
        }
        let scaleX = containerFrame.width / viewport.width
        let scaleY = containerFrame.height / viewport.height
        let scale = min(scaleX, scaleY)
        let w = viewport.width * scale
        let h = viewport.height * scale
        let x = (containerFrame.width - w) / 2
        let y = (containerFrame.height - h) / 2
        return CGRect(x: x, y: y, width: w, height: h)
    }

    /// Convert an absolute viewport point (px) to a local view point.
    func viewPoint(fromViewport vp: CGPoint) -> CGPoint {
        let rect = renderedRect
        guard rect.width > 0, rect.height > 0 else { return .zero }
        let scale = rect.width / viewport.width
        return CGPoint(
            x: rect.origin.x + vp.x * scale,
            y: rect.origin.y + vp.y * scale
        )
    }
}

// MARK: - Computer View (Operator Console)

struct ComputerView: View {
    @EnvironmentObject var vm: ChatViewModel

    private var state: ComputerState { vm.computerState }

    var body: some View {
        Group {
            if let session = state.session {
                sessionView(session)
            } else {
                emptyState
            }
        }
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 16) {
            Spacer()
            Image(systemName: "desktopcomputer")
                .font(.system(size: 32, weight: .thin))
                .foregroundColor(Color.white.opacity(0.18))
            Text("No Active Session")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(BossColor.textPrimary)
            Text("Computer-use sessions will appear here when active.\nBoss drives a browser, you spectate and approve.")
                .font(.system(size: 13))
                .foregroundColor(Color.white.opacity(0.38))
                .multilineTextAlignment(.center)
                .lineSpacing(4)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Active Session

    private func sessionView(_ session: ComputerSessionInfo) -> some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) {
                statusHeader(session)
                    .padding(.bottom, 20)

                screenshotConsole(session)
                    .padding(.bottom, 16)

                telemetryBar(session)
                    .padding(.bottom, 20)

                actionControls(session)
                    .padding(.bottom, 24)

                Divider().background(BossColor.divider)
                    .padding(.bottom, 20)

                timelineSection
            }
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .padding(.bottom, 40)
            .frame(maxWidth: 680)
            .frame(maxWidth: .infinity)
        }
    }

    // MARK: - Status Header

    private func statusHeader(_ session: ComputerSessionInfo) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text("Computer")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundColor(BossColor.textPrimary)
                Spacer()
                statusPill(session.status)
            }

            if let task = session.task {
                Text(task)
                    .font(.system(size: 12, weight: .regular))
                    .foregroundColor(Color.white.opacity(0.4))
                    .lineLimit(2)
            }

            HStack(spacing: 6) {
                Image(systemName: "globe")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(BossColor.textSecondary)
                    .frame(width: 14)
                Text(session.currentDomain ?? session.targetDomain ?? session.targetUrl ?? "—")
                    .font(.system(size: 13, weight: .medium, design: .monospaced))
                    .foregroundColor(BossColor.textSecondary)
                    .lineLimit(1)
            }

            HStack(spacing: 6) {
                Image(systemName: "cpu")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.3))
                    .frame(width: 14)
                Text(session.activeModel)
                    .font(.system(size: 12, weight: .regular, design: .monospaced))
                    .foregroundColor(Color.white.opacity(0.3))
            }
        }
    }

    private func statusPill(_ status: ComputerSessionStatus) -> some View {
        HStack(spacing: 6) {
            switch status {
            case .running:
                PulsingDot()
            case .paused:
                Image(systemName: "pause.fill")
                    .font(.system(size: 8))
                    .foregroundColor(Color.orange)
            case .waitingApproval:
                PulsingDot(color: Color(hex: "#FBBF24"))
            default:
                Circle()
                    .fill(status.color)
                    .frame(width: 7, height: 7)
            }
            Text(status == .paused ? "Takeover" : status.label)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(status.color)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(status.color.opacity(0.12))
        .clipShape(Capsule())
    }

    // MARK: - Screenshot Console (with overlays)

    private func screenshotConsole(_ session: ComputerSessionInfo) -> some View {
        ZStack {
            screenshotImage(session)

            // Dim overlay for approval state
            if session.status == .waitingApproval {
                Color.black.opacity(0.4)
                    .allowsHitTesting(false)
                    .transition(.opacity)
            }

            // Action coordinate overlays
            GeometryReader { geo in
                actionOverlays(session: session, containerSize: geo.size)
            }

            // Approval gate overlay
            if session.status == .waitingApproval {
                approvalGate(session)
                    .transition(.opacity.combined(with: .scale(scale: 0.97)))
            }

            // Paused takeover indicator
            if session.status == .paused {
                pausedOverlay(session)
                    .transition(.opacity)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(session.status == .waitingApproval
                        ? Color(hex: "#FBBF24").opacity(0.5)
                        : Color(hex: "#27272A"),
                        lineWidth: session.status == .waitingApproval ? 1.5 : 1)
        )
        .shadow(color: .black.opacity(0.4), radius: 12, y: 4)
        .animation(.easeInOut(duration: 0.15), value: session.status)
    }

    @ViewBuilder
    private func screenshotImage(_ session: ComputerSessionInfo) -> some View {
        if let nsImage = state.screenshotImage {
            Image(nsImage: nsImage)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(maxWidth: .infinity)
        } else if let path = session.latestScreenshotPath,
                  let nsImage = NSImage(contentsOfFile: path) {
            Image(nsImage: nsImage)
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(maxWidth: .infinity)
        } else {
            Rectangle()
                .fill(Color(hex: "#18181B"))
                .aspectRatio(16.0 / 10.0, contentMode: .fit)
                .overlay(
                    VStack(spacing: 8) {
                        Image(systemName: "photo")
                            .font(.system(size: 24, weight: .thin))
                            .foregroundColor(Color.white.opacity(0.12))
                        Text("Screenshot will appear here")
                            .font(.system(size: 11))
                            .foregroundColor(Color.white.opacity(0.2))
                    }
                )
        }
    }

    // MARK: - Action Overlays

    private func actionOverlays(session: ComputerSessionInfo, containerSize: CGSize) -> some View {
        let mapper = CoordinateMapper(
            viewport: state.viewportSize,
            containerFrame: containerSize
        )
        return ZStack {
            ForEach(session.lastActionBatch) { action in
                if let x = action.x, let y = action.y {
                    let pt = mapper.viewPoint(fromViewport: CGPoint(x: CGFloat(x), y: CGFloat(y)))
                    ActionCrosshair(
                        actionType: action.type,
                        position: pt
                    )
                }
            }
        }
        .allowsHitTesting(false)
    }

    // MARK: - Approval Gate

    private func approvalGate(_ session: ComputerSessionInfo) -> some View {
        VStack(spacing: 0) {
            Spacer()

            VStack(spacing: 14) {
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 14))
                        .foregroundColor(Color(hex: "#FBBF24"))
                    Text("APPROVAL REQUIRED")
                        .font(.system(size: 12, weight: .bold, design: .monospaced))
                        .foregroundColor(Color(hex: "#FBBF24"))
                        .tracking(1.4)
                }

                // Show pending actions
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(session.lastActionBatch) { action in
                        Text(action.summary)
                            .font(.system(size: 11, weight: .medium, design: .monospaced))
                            .foregroundColor(BossColor.textPrimary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
                .background(Color.white.opacity(0.04))
                .clipShape(RoundedRectangle(cornerRadius: 6))

                HStack(spacing: 10) {
                    // Deny — solid accent
                    Button {
                        Task { await state.approve(decision: "deny") }
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "xmark")
                                .font(.system(size: 10, weight: .bold))
                            Text("Deny")
                                .font(.system(size: 12, weight: .semibold))
                        }
                        .foregroundColor(.white)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(BossColor.accent)
                        .clipShape(RoundedRectangle(cornerRadius: 7))
                    }
                    .buttonStyle(.plain)

                    // Allow — ghost button
                    Button {
                        Task { await state.approve(decision: "allow") }
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "checkmark")
                                .font(.system(size: 10, weight: .bold))
                            Text("Allow")
                                .font(.system(size: 12, weight: .semibold))
                        }
                        .foregroundColor(BossColor.textSecondary)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(
                            RoundedRectangle(cornerRadius: 7)
                                .stroke(Color.white.opacity(0.15), lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)

                    Spacer()
                }
            }
            .padding(16)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color(hex: "#141414").opacity(0.95))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(Color(hex: "#FBBF24").opacity(0.25), lineWidth: 1)
            )
            .padding(12)
        }
    }

    // MARK: - Paused / Takeover Overlay

    private func pausedOverlay(_ session: ComputerSessionInfo) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "hand.raised.fill")
                    .font(.system(size: 12))
                    .foregroundColor(.orange)
                Text("OPERATOR TAKEOVER")
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundColor(.orange)
                    .tracking(1.2)
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(Color.orange.opacity(0.08))

            Spacer()
        }
    }

    // MARK: - Telemetry Bar

    private func telemetryBar(_ session: ComputerSessionInfo) -> some View {
        HStack(spacing: 0) {
            telemetryCell("TURN", value: "\(session.turnIndex)")
            telemetryDivider
            telemetryCell("ACTIONS", value: "\(session.lastActionBatch.count)")
            telemetryDivider
            telemetryCell("STATUS", value: lastActionOutcome(session))
            telemetryDivider
            telemetryCell("ELAPSED", value: elapsed(since: session.createdAt))
            telemetryDivider
            safetyCell(session)

            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .background(Color.white.opacity(0.025))
        .clipShape(RoundedRectangle(cornerRadius: 7))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(Color.white.opacity(0.06), lineWidth: 1)
        )
    }

    private func safetyCell(_ session: ComputerSessionInfo) -> some View {
        let domain = session.currentDomain ?? session.targetDomain ?? "unknown"
        return VStack(alignment: .leading, spacing: 2) {
            Text("SAFETY")
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundColor(Color.white.opacity(0.25))
                .tracking(1.2)
            Text(session.domainAllowlisted ? "Allowlisted" : "Unverified")
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .foregroundColor(session.domainAllowlisted
                    ? Color(hex: "#34D399")
                    : Color(hex: "#FBBF24"))
        }
        .frame(minWidth: 80, alignment: .leading)
        .help("Domain: \(domain)")
    }

    private func telemetryCell(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                .foregroundColor(Color.white.opacity(0.25))
                .tracking(1.2)
            Text(value)
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .foregroundColor(BossColor.textPrimary)
        }
        .frame(minWidth: 70, alignment: .leading)
    }

    private var telemetryDivider: some View {
        Rectangle()
            .fill(Color.white.opacity(0.06))
            .frame(width: 1, height: 28)
            .padding(.horizontal, 10)
    }

    // MARK: - Action Controls

    private func actionControls(_ session: ComputerSessionInfo) -> some View {
        HStack(spacing: 8) {
            if session.status == .running {
                ghostButton("Pause", icon: "pause.fill") {
                    Task { await state.pause() }
                }
            }
            if session.status == .paused {
                accentButton("Resume Agent", icon: "play.fill") {
                    Task { await state.resume() }
                }
            }
            if !session.status.isTerminal {
                destructiveButton("Stop", icon: "stop.fill") {
                    Task { await state.cancel() }
                }
            }

            Spacer()

            if session.status == .paused, let url = session.targetUrl, let u = URL(string: url) {
                ghostButton("Open in Browser", icon: "arrow.up.forward.square") {
                    NSWorkspace.shared.open(u)
                }
            }

            ghostButton("Refresh", icon: "arrow.clockwise") {
                Task {
                    await state.refresh(sessionId: session.sessionId)
                }
            }
        }
        .opacity(state.actionInFlight ? 0.5 : 1.0)
        .allowsHitTesting(!state.actionInFlight)
    }

    private func ghostButton(_ label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .semibold))
                    .frame(width: 14)
                Text(label)
                    .font(.system(size: 12, weight: .medium))
            }
            .foregroundColor(Color.white.opacity(0.55))
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: 7)
                    .stroke(Color.white.opacity(0.12), lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func accentButton(_ label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .semibold))
                    .frame(width: 14)
                Text(label)
                    .font(.system(size: 12, weight: .medium))
            }
            .foregroundColor(.white)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(Color(hex: "#34D399"))
            .clipShape(RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func destructiveButton(_ label: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .semibold))
                    .frame(width: 14)
                Text(label)
                    .font(.system(size: 12, weight: .medium))
            }
            .foregroundColor(.white)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(BossColor.accent)
            .clipShape(RoundedRectangle(cornerRadius: 7))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    // MARK: - Timeline

    private var timelineSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("TIMELINE")
                .font(.system(size: 11, weight: .medium))
                .foregroundColor(Color.white.opacity(0.25))
                .tracking(1.8)
                .padding(.bottom, 4)

            if state.events.isEmpty {
                Text("No events yet")
                    .font(.system(size: 12))
                    .foregroundColor(Color.white.opacity(0.25))
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(state.events.reversed()) { event in
                        timelineRow(event)
                    }
                }
            }
        }
    }

    private func timelineRow(_ event: ComputerEventInfo) -> some View {
        HStack(alignment: .top, spacing: 10) {
            // Timestamp
            Text(timelineTimestamp(event.timestamp))
                .font(.system(size: 10, weight: .regular, design: .monospaced))
                .foregroundColor(Color.white.opacity(0.2))
                .frame(width: 52, alignment: .trailing)

            // Actor icon: agent vs operator
            Image(systemName: event.isOperatorEvent ? "person.fill" : "desktopcomputer")
                .font(.system(size: 9, weight: .medium))
                .foregroundColor(event.isOperatorEvent
                    ? Color(hex: "#FBBF24")
                    : Color.white.opacity(0.2))
                .frame(width: 14)

            // Dot connector
            VStack(spacing: 0) {
                Circle()
                    .fill(eventDotColor(event.event))
                    .frame(width: 5, height: 5)
                    .padding(.top, 4)
                Rectangle()
                    .fill(Color.white.opacity(0.06))
                    .frame(width: 1)
                    .frame(minHeight: 16)
            }
            .frame(width: 5)

            // Content
            VStack(alignment: .leading, spacing: 2) {
                Text(eventLabel(event.event))
                    .font(.system(size: 12, weight: .medium))
                    .foregroundColor(BossColor.textSecondary)
                if let detail = event.detail {
                    Text(detail)
                        .font(.system(size: 11, weight: .regular, design: .monospaced))
                        .foregroundColor(Color.white.opacity(0.25))
                        .lineLimit(2)
                }
            }
            .padding(.bottom, 8)

            Spacer(minLength: 0)
        }
    }

    // MARK: - Helpers

    private func lastActionOutcome(_ session: ComputerSessionInfo) -> String {
        guard let last = session.lastActionResults.last else { return "—" }
        return last.success ? "OK" : "FAIL"
    }

    private func elapsed(since date: Date) -> String {
        let seconds = Int(Date().timeIntervalSince(date))
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        let secs = seconds % 60
        return "\(minutes)m \(secs)s"
    }

    private func timelineTimestamp(_ date: Date) -> String {
        let fmt = DateFormatter()
        fmt.dateFormat = "HH:mm:ss"
        return fmt.string(from: date)
    }

    private func eventDotColor(_ event: String) -> Color {
        switch event {
        case "error", "budget_exhausted":
            return BossColor.accent
        case "completed":
            return Color(hex: "#34D399")
        case "approval_granted", "approval_denied", "approval_resumed", "paused", "cancelled":
            return Color(hex: "#FBBF24")
        case "action_executed":
            return Color.white.opacity(0.25)
        default:
            return Color.white.opacity(0.12)
        }
    }

    private func eventLabel(_ event: String) -> String {
        event.replacingOccurrences(of: "_", with: " ").localizedCapitalized
    }
}

// MARK: - Action Crosshair

/// Radar-like crosshair overlay for a single action point.
private struct ActionCrosshair: View {
    let actionType: String
    let position: CGPoint

    @State private var rippleScale: CGFloat = 0.5
    @State private var rippleOpacity: Double = 0.8

    var body: some View {
        ZStack {
            // Ripple ring
            Circle()
                .stroke(BossColor.accent.opacity(rippleOpacity), lineWidth: 0.5)
                .frame(width: 24, height: 24)
                .scaleEffect(rippleScale)

            // Outer ring
            Circle()
                .stroke(BossColor.accent, lineWidth: 1)
                .frame(width: 12, height: 12)

            // Crosshair lines
            Rectangle()
                .fill(BossColor.accent.opacity(0.4))
                .frame(width: 1, height: 18)
            Rectangle()
                .fill(BossColor.accent.opacity(0.4))
                .frame(width: 18, height: 1)

            // Center dot
            Circle()
                .fill(BossColor.accent)
                .frame(width: 3, height: 3)
        }
        .position(position)
        .onAppear {
            withAnimation(.easeOut(duration: 0.6)) {
                rippleScale = 1.8
                rippleOpacity = 0.0
            }
        }
    }
}

// MARK: - Pulsing Dot

private struct PulsingDot: View {
    var color: Color = BossColor.accent
    @State private var isPulsing = false

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(0.3))
                .frame(width: 12, height: 12)
                .scaleEffect(isPulsing ? 1.4 : 0.8)
                .opacity(isPulsing ? 0.0 : 0.6)
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: false)) {
                isPulsing = true
            }
        }
    }
}

// MARK: - Active Session Pip

struct ComputerSessionPip: View {
    let domain: String
    let status: ComputerSessionStatus
    let onTap: () -> Void

    @State private var isHovered = false

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 7) {
                if status == .running {
                    PulsingDot()
                        .scaleEffect(0.7)
                } else if status == .waitingApproval {
                    PulsingDot(color: Color(hex: "#FBBF24"))
                        .scaleEffect(0.7)
                } else if status == .paused {
                    Image(systemName: "pause.fill")
                        .font(.system(size: 8))
                        .foregroundColor(.orange)
                } else {
                    Circle()
                        .fill(status.color)
                        .frame(width: 6, height: 6)
                }
                Image(systemName: "desktopcomputer")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundColor(Color.white.opacity(0.7))
                    .frame(width: 14)
                Text(domain)
                    .font(.system(size: 11, weight: .medium, design: .monospaced))
                    .foregroundColor(Color.white.opacity(0.6))
                    .lineLimit(1)

                if status == .waitingApproval {
                    Text("ACTION NEEDED")
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundColor(Color(hex: "#FBBF24"))
                        .tracking(0.8)
                }
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(
                Capsule()
                    .fill(Color.white.opacity(isHovered ? 0.08 : 0.04))
            )
            .overlay(
                Capsule()
                    .stroke(status == .waitingApproval
                            ? Color(hex: "#FBBF24").opacity(0.3)
                            : Color.white.opacity(0.08),
                            lineWidth: 1)
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .onHover { isHovered = $0 }
        .animation(.easeInOut(duration: 0.15), value: isHovered)
    }
}
