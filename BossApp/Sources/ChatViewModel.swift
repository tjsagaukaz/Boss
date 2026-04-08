import SwiftUI

// MARK: - Chat ViewModel

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var messages: [ChatMessage] = []
    @Published var inputText: String = ""
    @Published var isLoading: Bool = false
    @Published var currentAgent: String = AgentInfo.entryAgentName
    @Published var activeToolName: String?
    @Published var pendingPermissionCount: Int = 0
    @Published var sessionId: String = UUID().uuidString
    @Published var selectedSurface: AppSurface = .chat
    @Published var selectedProjectPath: String?

    // Sidebar data
    @Published var projects: [ProjectInfo] = []
    @Published var facts: [FactInfo] = []
    @Published var memoryStats: MemoryStats?
    @Published var memoryOverview: MemoryOverview?
    @Published var permissions: [PermissionEntry] = []
    @Published var sidebarRefreshError: String?
    @Published var memoryRefreshError: String?
    @Published var permissionsRefreshError: String?
    @Published var startupIssue: String?

    private let api = APIClient.shared

    init() {
        messages.append(ChatMessage(role: .system, content: "Boss Assistant ready. Ask me anything."))
        Task { await bootstrapRuntimeAndRefresh() }
    }

    // MARK: - Send message

    func send() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isLoading else { return }

        inputText = ""
        selectedSurface = .chat
        messages.append(ChatMessage(role: .user, content: text))

        let assistantMsg = ChatMessage(role: .assistant, content: "", agent: AgentInfo.entryAgentName, isStreaming: true)
        messages.append(assistantMsg)

        isLoading = true
        currentAgent = AgentInfo.entryAgentName

        Task {
            guard await ensureBackendReadyForUserAction(messageId: assistantMsg.id) else {
                return
            }
            await consumeStream(
                api.streamChat(message: text, sessionId: sessionId),
                for: assistantMsg.id
            )
        }
    }

    func respondToPermission(
        messageId: UUID,
        request: PermissionRequest,
        decision: PermissionDecision
    ) {
        guard let messageIndex = messageIndex(for: messageId),
              let stepIndex = executionStepIndex(in: messageIndex, stepId: request.approvalId) else {
            return
        }

        messages[messageIndex].executionSteps[stepIndex].decision = decision
        messages[messageIndex].executionSteps[stepIndex].permissionRequest = nil
        messages[messageIndex].executionSteps[stepIndex].state = decision == .deny ? .failure : .running
        messages[messageIndex].isStreaming = true

        isLoading = true
        activeToolName = messages[messageIndex].executionSteps[stepIndex].title
        refreshPermissionCount()

        Task {
            guard await ensureBackendReadyForResumedAction(messageId: messageId) else {
                return
            }
            await consumeStream(
                api.streamPermissionDecision(
                    runId: request.runId,
                    approvalId: request.approvalId,
                    decision: decision
                ),
                for: messageId
            )
        }
    }

    private func consumeStream(_ stream: AsyncStream<SSEEvent>, for messageId: UUID) async {
        var sawDone = false

        for await event in stream {
            if handleEvent(event, for: messageId) {
                sawDone = true
            }
        }

        if let messageIndex = messageIndex(for: messageId) {
            messages[messageIndex].isStreaming = false
        }

        refreshPermissionCount()
        isLoading = false
        activeToolName = nil

        if sawDone {
            await refreshSidebar()
            await refreshPermissions()
        }
    }

    @discardableResult
    private func handleEvent(_ event: SSEEvent, for messageId: UUID) -> Bool {
        guard let messageIndex = messageIndex(for: messageId) else { return false }

        switch event.type {
        case "session":
            if let sid = event.data["session_id"] {
                sessionId = sid
            }

        case "agent":
            if let name = event.data["name"] {
                currentAgent = name
                messages[messageIndex].agent = name
                activeToolName = nil
            }

        case "text":
            if let content = event.data["content"] {
                messages[messageIndex].content += content
            }

        case "thinking":
            if let content = event.data["content"] {
                messages[messageIndex].thinkingContent = content
            }

        case "handoff":
            markLatestTransferStepSuccessful(in: messageIndex)
            let from = event.data["from"] ?? "?"
            let to = event.data["to"] ?? "?"
            let info = AgentInfo.forName(to)
            messages[messageIndex].executionSteps.append(
                ExecutionStep(
                    id: UUID().uuidString,
                    kind: .handoff,
                    name: "handoff",
                    title: "Handoff",
                    description: "\(from) → \(info.display)",
                    state: .success
                )
            )

        case "tool_call":
            let callId = event.data["call_id"] ?? UUID().uuidString
            let name = event.data["name"] ?? "tool"
            let title = event.data["title"] ?? name.replacingOccurrences(of: "_", with: " ").capitalized
            let description = event.data["description"] ?? title
            let arguments = event.data["arguments"] ?? ""
            let executionType = event.data["execution_type"].flatMap(ExecutionType.init(rawValue:))
            let state: ToolState = executionType == .plan
                ? .running
                : executionType?.requiresPermission == true ? .pending : .running
            let step = ExecutionStep(
                id: callId,
                kind: .tool,
                name: name,
                title: title,
                description: description,
                arguments: arguments,
                state: state,
                executionType: executionType
            )

            if let stepIndex = executionStepIndex(in: messageIndex, stepId: callId) {
                messages[messageIndex].executionSteps[stepIndex] = step
            } else {
                messages[messageIndex].executionSteps.append(step)
            }
            activeToolName = title

        case "tool_result":
            let callId = event.data["call_id"] ?? ""
            let output = event.data["output"]
            if let stepIndex = executionStepIndex(in: messageIndex, stepId: callId)
                ?? lastToolStepIndex(in: messageIndex) {
                messages[messageIndex].executionSteps[stepIndex].output = output
                messages[messageIndex].executionSteps[stepIndex].state =
                    messages[messageIndex].executionSteps[stepIndex].decision == .deny ? .failure : .success
                messages[messageIndex].executionSteps[stepIndex].permissionRequest = nil
            }
            activeToolName = nil

        case "permission_request":
            let runId = event.data["run_id"] ?? ""
            let approvalId = event.data["approval_id"] ?? UUID().uuidString
            let name = event.data["tool"] ?? "tool"
            let title = event.data["title"] ?? name.replacingOccurrences(of: "_", with: " ").capitalized
            let description = event.data["description"] ?? title
            let executionType = event.data["execution_type"].flatMap(ExecutionType.init(rawValue:)) ?? .run
            let scopeLabel = event.data["scope_label"] ?? "Any"
            let request = PermissionRequest(
                runId: runId,
                approvalId: approvalId,
                name: name,
                title: title,
                description: description,
                executionType: executionType,
                scopeLabel: scopeLabel
            )

            if let stepIndex = executionStepIndex(in: messageIndex, stepId: approvalId) {
                messages[messageIndex].executionSteps[stepIndex].title = title
                messages[messageIndex].executionSteps[stepIndex].description = description
                messages[messageIndex].executionSteps[stepIndex].executionType = executionType
                messages[messageIndex].executionSteps[stepIndex].permissionRequest = request
                messages[messageIndex].executionSteps[stepIndex].state = .waitingPermission
            } else {
                messages[messageIndex].executionSteps.append(
                    ExecutionStep(
                        id: approvalId,
                        kind: .tool,
                        name: name,
                        title: title,
                        description: description,
                        state: .waitingPermission,
                        executionType: executionType,
                        permissionRequest: request
                    )
                )
            }

            messages[messageIndex].isStreaming = false
            refreshPermissionCount()

        case "permission_result":
            guard let approvalId = event.data["approval_id"],
                  let decisionRaw = event.data["decision"],
                  let decision = PermissionDecision(rawValue: decisionRaw),
                  let stepIndex = executionStepIndex(in: messageIndex, stepId: approvalId) else {
                break
            }

            messages[messageIndex].executionSteps[stepIndex].decision = decision
            messages[messageIndex].executionSteps[stepIndex].permissionRequest = nil
            messages[messageIndex].executionSteps[stepIndex].state = decision == .deny ? .failure : .running
            refreshPermissionCount()

        case "error":
            let message = event.data["message"] ?? "Unknown error"
            if messages[messageIndex].content.isEmpty {
                messages[messageIndex].content = message
            }
            if let stepIndex = lastToolStepIndex(in: messageIndex) {
                messages[messageIndex].executionSteps[stepIndex].state = .failure
                messages[messageIndex].executionSteps[stepIndex].output = message
            }
            messages[messageIndex].isStreaming = false

        case "done":
            messages[messageIndex].isStreaming = false
            return true

        default:
            break
        }

        return false
    }

    private func messageIndex(for messageId: UUID) -> Int? {
        messages.firstIndex { $0.id == messageId }
    }

    private func executionStepIndex(in messageIndex: Int, stepId: String) -> Int? {
        messages[messageIndex].executionSteps.firstIndex { $0.id == stepId }
    }

    private func lastToolStepIndex(in messageIndex: Int) -> Int? {
        messages[messageIndex].executionSteps.lastIndex { $0.kind == .tool }
    }

    private func markLatestTransferStepSuccessful(in messageIndex: Int) {
        guard let stepIndex = messages[messageIndex].executionSteps.lastIndex(where: {
            $0.kind == .tool && $0.name.hasPrefix("transfer_to_") && $0.state != .success
        }) else {
            return
        }
        messages[messageIndex].executionSteps[stepIndex].state = .success
    }

    private func refreshPermissionCount() {
        pendingPermissionCount = messages
            .flatMap(\.executionSteps)
            .filter { $0.state == .waitingPermission && $0.permissionRequest != nil }
            .count
    }

    // MARK: - Sidebar refresh

    func refreshSidebar() async {
        var failures: [String] = []

        do {
            projects = try await api.fetchProjects()
        } catch {
            failures.append("Projects unavailable: \(errorMessage(error))")
        }

        do {
            facts = try await api.fetchFacts()
        } catch {
            failures.append("Memory facts unavailable: \(errorMessage(error))")
        }

        do {
            memoryStats = try await api.fetchStats()
        } catch {
            failures.append("Memory stats unavailable: \(errorMessage(error))")
        }

        do {
            memoryOverview = try await api.fetchMemoryOverview(
                sessionId: sessionId,
                message: currentMemoryPreviewMessage()
            )
        } catch {
            failures.append("Memory preview unavailable: \(errorMessage(error))")
        }

        if failures.isEmpty {
            sidebarRefreshError = nil
        } else {
            sidebarRefreshError = "Sidebar refresh incomplete. \(failures.joined(separator: "  "))"
        }
    }

    func refreshMemoryOverview(messageOverride: String? = nil) async {
        do {
            memoryOverview = try await api.fetchMemoryOverview(
                sessionId: sessionId,
                message: memoryPreviewMessage(messageOverride)
            )
            memoryRefreshError = nil
        } catch {
            memoryRefreshError = "Memory refresh failed. \(errorMessage(error))"
        }
    }

    func refreshPermissions() async {
        do {
            permissions = try await api.fetchPermissions()
            permissionsRefreshError = nil
        } catch {
            permissionsRefreshError = "Permissions refresh failed. \(errorMessage(error))"
        }
    }

    func showChat() {
        selectedSurface = .chat
    }

    func showMemory(projectPath: String? = nil) {
        selectedProjectPath = projectPath
        selectedSurface = .memory
        Task { await refreshMemoryOverview() }
    }

    func showPermissions() {
        selectedSurface = .permissions
        Task { await refreshPermissions() }
    }

    func revokePermission(_ entry: PermissionEntry) {
        let previousPermissions = permissions
        permissions.removeAll { $0.id == entry.id }

        Task {
            do {
                try await api.revokePermission(tool: entry.tool, scopeKey: entry.scopeKey)
                await refreshPermissions()
            } catch {
                permissions = previousPermissions
                permissionsRefreshError = "Couldn't revoke permission. \(errorMessage(error))"
            }
        }
    }

    func forgetMemory(sourceTable: String, itemId: Int) {
        Task {
            do {
                try await api.deleteMemoryItem(sourceTable: sourceTable, itemId: itemId)
                memoryRefreshError = nil
                await refreshSidebar()
            } catch {
                memoryRefreshError = "Couldn't update memory. \(errorMessage(error))"
            }
        }
    }

    func newSession() {
        messages = [ChatMessage(role: .system, content: "New session started.")]
        sessionId = UUID().uuidString
        pendingPermissionCount = 0
        activeToolName = nil
        isLoading = false
        currentAgent = AgentInfo.entryAgentName
        selectedProjectPath = nil
        memoryOverview = nil
        sidebarRefreshError = nil
        memoryRefreshError = nil
        permissionsRefreshError = nil
        selectedSurface = .chat
        Task { await refreshSidebar() }
    }

    func scanSystem(showChat: Bool = false) {
        if showChat {
            selectedSurface = .chat
        }
        Task {
            do {
                let result = try await api.triggerScan()
                let found = result["projects_found"] as? Int ?? 0
                let updated = result["projects_updated"] as? Int ?? 0
                let filesIndexed = result["files_indexed"] as? Int ?? 0
                let summariesRefreshed = result["summaries_refreshed"] as? Int ?? 0
                messages.append(
                    ChatMessage(
                        role: .system,
                        content: "System scan complete. Found \(found) projects, updated \(updated), indexed \(filesIndexed) files, refreshed \(summariesRefreshed) summaries."
                    )
                )
                await refreshSidebar()
            } catch {
                messages.append(ChatMessage(role: .error, content: "Scan failed: \(error.localizedDescription)"))
            }
        }
    }

    private func currentMemoryPreviewMessage() -> String? {
        memoryPreviewMessage(nil)
    }

    private func bootstrapRuntimeAndRefresh() async {
        let bootstrap = await LocalBackendBootstrapper.shared.ensureBackendReady(api: api)
        switch bootstrap {
        case .ready, .started:
            startupIssue = nil
        case .warning(let message), .failure(let message):
            startupIssue = message
        }

        await refreshSidebar()
        await refreshPermissions()
    }

    private func ensureBackendReadyForUserAction(messageId: UUID) async -> Bool {
        let bootstrap = await LocalBackendBootstrapper.shared.ensureBackendReady(api: api)
        switch bootstrap {
        case .ready, .started:
            startupIssue = nil
            return true
        case .warning(let message):
            startupIssue = message
            return true
        case .failure(let message):
            startupIssue = message
            if let index = messageIndex(for: messageId) {
                messages.remove(at: index)
            }
            messages.append(ChatMessage(role: .error, content: message))
            isLoading = false
            activeToolName = nil
            return false
        }
    }

    private func ensureBackendReadyForResumedAction(messageId: UUID) async -> Bool {
        let bootstrap = await LocalBackendBootstrapper.shared.ensureBackendReady(api: api)
        switch bootstrap {
        case .ready, .started:
            startupIssue = nil
            return true
        case .warning(let message):
            startupIssue = message
            return true
        case .failure(let message):
            startupIssue = message
            if let index = messageIndex(for: messageId) {
                messages[index].isStreaming = false
            }
            messages.append(ChatMessage(role: .error, content: message))
            isLoading = false
            activeToolName = nil
            return false
        }
    }

    private func errorMessage(_ error: Error) -> String {
        if let apiError = error as? APIError {
            return apiError.userMessage
        }
        return error.localizedDescription
    }

    private func memoryPreviewMessage(_ messageOverride: String?) -> String? {
        if let override = messageOverride?.trimmingCharacters(in: .whitespacesAndNewlines), !override.isEmpty {
            return override
        }

        let draft = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        if !draft.isEmpty {
            return draft
        }

        return messages.last(where: { $0.role == .user })?.content
    }
}
