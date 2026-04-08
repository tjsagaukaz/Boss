import SwiftUI

// MARK: - Typography Tokens

private enum Typo {
    static let primaryText   = Color.white.opacity(0.92)
    static let secondaryText = Color.white.opacity(0.55)
    static let tertiaryText  = Color.white.opacity(0.35)

    static let bodySize: CGFloat   = 15
    static let lineGap: CGFloat    = 7
    static let tracking: CGFloat   = -0.15
    static let paragraphGap: CGFloat = 12
}

// MARK: - Chat View

struct ChatView: View {
    @EnvironmentObject var vm: ChatViewModel
    @FocusState private var inputFocused: Bool

    private var hasRealMessages: Bool {
        vm.messages.contains { $0.role == .user || $0.role == .assistant }
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView(.vertical, showsIndicators: false) {
                    VStack(spacing: 0) {
                        // Persistent header anchor
                        if hasRealMessages {
                            Text("Boss")
                                .font(.system(size: 12, weight: .medium))
                                .foregroundColor(Typo.tertiaryText)
                                .tracking(0.3)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.leading, 4)
                                .padding(.top, 8)
                                .padding(.bottom, 4)
                        } else {
                            VStack(spacing: 8) {
                                Text("Boss")
                                    .font(.system(size: 22, weight: .semibold))
                                    .foregroundColor(Typo.primaryText)
                                    .tracking(-0.3)
                                Text("Ready. Ask anything.")
                                    .font(.system(size: 14))
                                    .foregroundColor(Typo.tertiaryText)
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.top, 120)
                            .padding(.bottom, 60)
                        }

                        // Message flow
                        VStack(alignment: .leading, spacing: 0) {
                            ForEach(Array(vm.messages.enumerated()), id: \.element.id) { index, message in
                                if message.role != .system {
                                    MessageView(
                                        message: message,
                                        previousRole: previousRole(at: index)
                                    )
                                    .id(message.id)
                                }
                            }
                        }
                    }
                    .frame(maxWidth: 680, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.top, hasRealMessages ? 80 : 0)
                    .padding(.bottom, 32)
                }
                .onChange(of: vm.messages.count) { _, _ in
                    if let last = vm.messages.last {
                        withAnimation(.easeOut(duration: 0.14)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
                .onChange(of: vm.messages.last?.content) { _, _ in
                    if let last = vm.messages.last, last.isStreaming {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }

            // System state strip
            systemStateBar
                .frame(maxWidth: 680)
                .frame(maxWidth: .infinity, alignment: .center)

            // Input
            inputBar
                .frame(maxWidth: 680)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.bottom, 32)
        }
        .background(
            ZStack {
                BossColor.black
                RadialGradient(
                    colors: [Color.white.opacity(0.015), .clear],
                    center: .center,
                    startRadius: 0,
                    endRadius: 600
                )
            }
            .ignoresSafeArea()
        )
    }

    private func previousRole(at index: Int) -> ChatMessage.Role? {
        let realMessages = vm.messages.filter { $0.role != .system }
        guard let currentIdx = realMessages.firstIndex(where: { $0.id == vm.messages[index].id }),
              currentIdx > 0 else { return nil }
        return realMessages[currentIdx - 1].role
    }

    // MARK: - System State Bar

    private var systemStateBar: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(
                    vm.pendingPermissionCount > 0
                        ? Color.white.opacity(0.5)
                        : vm.isLoading ? BossColor.accent.opacity(0.8) : Color.white.opacity(0.15)
                )
                .frame(width: 5, height: 5)

            if vm.pendingPermissionCount > 0 {
                Text("Awaiting approval")
                    .font(.system(size: 11))
                    .foregroundColor(Typo.secondaryText)
            } else if vm.isLoading {
                if let tool = vm.activeToolName {
                    Text(tool)
                        .font(.system(size: 11))
                        .foregroundColor(Typo.secondaryText)
                } else {
                    Text(vm.currentAgent == AgentInfo.entryAgentName ? "Boss is thinking…" : "\(AgentInfo.forName(vm.currentAgent).display) is thinking…")
                        .font(.system(size: 11))
                        .foregroundColor(Typo.secondaryText)
                }
            } else {
                Text("Idle")
                    .font(.system(size: 11))
                    .foregroundColor(Typo.tertiaryText)
            }

            Spacer()
        }
        .frame(height: 22)
        .padding(.horizontal, 24)
        .padding(.bottom, 6)
        .animation(.easeOut(duration: 0.14), value: vm.isLoading)
    }

    // MARK: - Input Bar

    private var hasText: Bool {
        !vm.inputText.trimmingCharacters(in: .whitespaces).isEmpty
    }

    private var inputBar: some View {
        VStack(spacing: 0) {
            // Text area
            TextField("Ask Boss...", text: $vm.inputText, axis: .vertical)
                .textFieldStyle(.plain)
                .font(.system(size: 16))
                .tracking(Typo.tracking)
                .foregroundColor(Typo.primaryText)
                .lineLimit(1...8)
                .focused($inputFocused)
                .onKeyPress(.return) {
                    if NSEvent.modifierFlags.contains(.shift) {
                        return .ignored
                    }
                    vm.send()
                    return .handled
                }
                .padding(.horizontal, 20)
                .padding(.top, 18)
                .padding(.bottom, 12)

            // Bottom toolbar row
            HStack(spacing: 12) {
                Button(action: {}) {
                    Image(systemName: "plus")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundColor(Typo.secondaryText)
                }
                .buttonStyle(.plain)

                Spacer()

                Button(action: { vm.send() }) {
                    Image(systemName: vm.isLoading ? "stop.fill" : "arrow.up")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundColor(hasText || vm.isLoading ? .white : Typo.tertiaryText)
                        .frame(width: 30, height: 30)
                        .background(
                            Circle()
                                .fill(hasText || vm.isLoading
                                    ? BossColor.accent
                                    : Color.white.opacity(0.06))
                        )
                }
                .buttonStyle(.plain)
                .keyboardShortcut(.return, modifiers: .command)
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 14)
        }
        .background(
            RoundedRectangle(cornerRadius: 26)
                .fill(Color.white.opacity(0.06))
        )
        .padding(.horizontal, 20)
    }
}

// MARK: - Message View (Editorial)

struct MessageView: View {
    @EnvironmentObject var vm: ChatViewModel
    let message: ChatMessage
    let previousRole: ChatMessage.Role?
    @State private var showThinking = false
    @State private var appeared = false

    // Spacing: tight pairing for user→assistant, standard otherwise
    private var topSpacing: CGFloat {
        guard let prev = previousRole else { return 0 }
        if prev == .user && message.role == .assistant { return 20 }
        if prev == .assistant && message.role == .assistant { return 12 }
        return 28
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            switch message.role {
            case .user:
                userMessage
            case .assistant:
                assistantMessage
            case .error:
                errorMessage
            case .system:
                EmptyView()
            }
        }
        .padding(.top, topSpacing)
        .opacity(appeared ? 1 : 0)
        .onAppear { withAnimation(.easeOut(duration: 0.12)) { appeared = true } }
    }

    // MARK: - User Message (left-rail aligned, subtle container)

    private var userMessage: some View {
        Text(message.content)
            .font(.system(size: Typo.bodySize))
            .tracking(Typo.tracking)
            .lineSpacing(Typo.lineGap)
            .foregroundColor(Typo.primaryText)
            .textSelection(.enabled)
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color.white.opacity(0.025))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(Color.white.opacity(0.04), lineWidth: 1)
            )
            .frame(maxWidth: 640, alignment: .leading)
    }

    // MARK: - Assistant Message (editorial text block)

    private var assistantMessage: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Agent label — quiet, uppercase
            if let agent = message.agent, agent != AgentInfo.entryAgentName {
                Text(agent.uppercased())
                    .font(.system(size: 10, weight: .medium))
                    .foregroundColor(Typo.tertiaryText)
                    .tracking(1.2)
                    .padding(.bottom, 6)
            }

            if !message.executionSteps.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(message.executionSteps) { step in
                        executionStepView(step)
                    }
                }
                .padding(.bottom, message.content.isEmpty ? 0 : 14)
            }

            // Content: crossfade from streaming plain text to block-parsed markdown
            if !message.content.isEmpty {
                ZStack(alignment: .topLeading) {
                    StreamingTextView(text: message.content)
                        .opacity(message.isStreaming ? 1 : 0)

                    if !message.isStreaming {
                        MarkdownBlocksView(blocks: MarkdownParser.parse(message.content))
                            .transition(.opacity)
                    }
                }
                .animation(.easeOut(duration: 0.12), value: message.isStreaming)
            }

            // Streaming dots
            if message.isStreaming && message.content.isEmpty {
                HStack(spacing: 5) {
                    Circle().fill(Typo.tertiaryText).frame(width: 3, height: 3)
                    Circle().fill(Typo.tertiaryText.opacity(0.6)).frame(width: 3, height: 3)
                    Circle().fill(Typo.tertiaryText.opacity(0.3)).frame(width: 3, height: 3)
                }
                .padding(.top, 4)
            }

            // Thinking
            if let thinking = message.thinkingContent, !thinking.isEmpty {
                Button(action: { showThinking.toggle() }) {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 8, weight: .semibold))
                            .rotationEffect(.degrees(showThinking ? 90 : 0))
                        Text("Reasoning")
                            .font(.system(size: 11))
                    }
                    .foregroundColor(Typo.tertiaryText)
                }
                .buttonStyle(.plain)
                .padding(.top, 10)
                .animation(.easeOut(duration: 0.14), value: showThinking)

                if showThinking {
                    Text(thinking)
                        .font(.system(size: 12, design: .monospaced))
                        .foregroundColor(Typo.tertiaryText)
                        .lineSpacing(4)
                        .tracking(0)
                        .padding(.leading, 12)
                        .padding(.top, 4)
                }
            }
        }
        .frame(maxWidth: 640, alignment: .leading)
    }

    // MARK: - Execution Narrative

    private func executionStepView(_ step: ExecutionStep) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(primaryLine(for: step))
                .font(.system(size: 13))
                .foregroundColor(step.state == .success ? Typo.tertiaryText : Typo.secondaryText)
                .contentTransition(.opacity)

            if let statusLine = secondaryLine(for: step) {
                Text(statusLine)
                    .font(.system(size: 12))
                    .foregroundColor(Typo.tertiaryText)
                    .contentTransition(.opacity)
            }

            if let request = step.permissionRequest, step.state == .waitingPermission {
                PermissionPromptView(request: request) { decision in
                    vm.respondToPermission(messageId: message.id, request: request, decision: decision)
                }
                .padding(.top, 4)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(.easeOut(duration: 0.12), value: step.state)
    }

    private func primaryLine(for step: ExecutionStep) -> String {
        switch step.kind {
        case .handoff:
            return step.description
        case .tool:
            let base = step.description.isEmpty ? step.title : step.description
            switch step.state {
            case .pending, .waitingPermission, .running:
                return base + "…"
            case .success, .failure:
                return base
            }
        }
    }

    private func secondaryLine(for step: ExecutionStep) -> String? {
        switch step.kind {
        case .handoff:
            return nil
        case .tool:
            switch step.state {
            case .pending:
                return "→ Pending"
            case .waitingPermission:
                return "→ Waiting for approval"
            case .running:
                return "→ Running"
            case .success:
                if let output = step.output, !output.isEmpty {
                    return "→ " + shortened(output)
                }
                return "→ Complete"
            case .failure:
                if step.decision == .deny {
                    return "→ Not approved"
                }
                if let output = step.output, !output.isEmpty {
                    return "→ " + shortened(output)
                }
                return "→ Failed"
            }
        }
    }

    private func shortened(_ value: String) -> String {
        let compact = value.replacingOccurrences(of: "\n", with: " ")
        let prefix = compact.prefix(72)
        return String(prefix) + (compact.count > 72 ? "…" : "")
    }

    // MARK: - Error

    private var errorMessage: some View {
        Text(message.content)
            .font(.system(size: 13))
            .tracking(Typo.tracking)
            .foregroundColor(BossColor.accent.opacity(0.8))
            .frame(maxWidth: 640, alignment: .leading)
    }
}
