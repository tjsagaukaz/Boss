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
    @Published var selectedMode: WorkMode = .default
    @Published var selectedExecutionStyle: ExecutionStyle = .singlePass
    @Published var draftAttachments: [AttachmentItem] = []
    @Published var selectedSurface: AppSurface = .chat
    @Published var selectedProjectPath: String?

    // Sidebar data
    @Published var projects: [ProjectInfo] = []
    @Published var facts: [FactInfo] = []
    @Published var memoryStats: MemoryStats?
    @Published var memoryOverview: MemoryOverview?
    @Published var systemStatus: SystemStatusInfo?
    @Published var jobs: [BackgroundJobInfo] = []
    @Published var selectedJob: BackgroundJobInfo?
    @Published var selectedJobLog: BackgroundJobLogTailInfo?
    @Published var reviewCapabilities: ReviewCapabilitiesInfo?
    @Published var reviewHistory: [ReviewRunInfo] = []
    @Published var selectedReviewRun: ReviewRunInfo?
    @Published var selectedReviewProjectPath: String?
    @Published var selectedReviewTarget: ReviewTargetKind = .auto
    @Published var reviewBaseRef: String = ""
    @Published var reviewHeadRef: String = ""
    @Published var reviewFilePathsText: String = ""
    @Published var isLaunchingBackgroundJob: Bool = false
    @Published var isRunningReview: Bool = false
    @Published var permissions: [PermissionEntry] = []
    @Published var sidebarRefreshError: String?
    @Published var memoryRefreshError: String?
    @Published var diagnosticsRefreshError: String?
    @Published var promptDiagnostics: PromptDiagnosticsInfo?
    @Published var jobsRefreshError: String?
    @Published var reviewRefreshError: String?
    @Published var permissionsRefreshError: String?
    @Published var previewStatus: PreviewStatusInfo?
    @Published var previewRefreshError: String?
    @Published var workPlans: [WorkPlanInfo] = []
    @Published var selectedWorkPlan: WorkPlanInfo?
    @Published var workersRefreshError: String?
    @Published var deployStatus: DeployStatusInfo?
    @Published var deployments: [DeploymentInfo] = []
    @Published var selectedDeployment: DeploymentInfo?
    @Published var deployRefreshError: String?
    @Published var startupIssue: String?

    private let api = APIClient.shared

    init() {
        messages.append(ChatMessage(role: .system, content: "Boss Assistant ready. Ask me anything."))
        Task { await bootstrapRuntimeAndRefresh() }
    }

    // MARK: - Send message

    func send() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachments = draftAttachments
        let requestText = requestMessage(text: text, attachments: attachments)
        guard !requestText.isEmpty, !isLoading else { return }

        inputText = ""
        draftAttachments = []
        selectedSurface = .chat
        messages.append(ChatMessage(role: .user, content: text, attachments: attachments))

        let assistantMsg = ChatMessage(role: .assistant, content: "", agent: AgentInfo.entryAgentName, isStreaming: true)
        messages.append(assistantMsg)

        isLoading = true
        currentAgent = AgentInfo.entryAgentName

        Task {
            guard await ensureBackendReadyForUserAction(messageId: assistantMsg.id) else {
                return
            }
            await consumeStream(
                api.streamChat(
                    message: requestText,
                    sessionId: sessionId,
                    mode: selectedMode,
                    projectPath: selectedProjectPath,
                    executionStyle: selectedExecutionStyle
                ),
                for: assistantMsg.id
            )
        }
    }

    func addAttachments(_ urls: [URL]) {
        for url in urls {
            let candidate = AttachmentItem(url: url)
            if draftAttachments.contains(where: { $0.path == candidate.path }) {
                continue
            }
            draftAttachments.append(candidate)
        }
    }

    func removeDraftAttachment(_ attachmentId: UUID) {
        draftAttachments.removeAll { $0.id == attachmentId }
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

        case "loop_status":
            let loopId = event.data["loop_id"] ?? ""
            let status = event.data["status"] ?? ""
            let stopReason = event.data["stop_reason"]
            let attempt = event.data["attempt"].flatMap { Int($0) }
            let task = event.data["task"]

            var budgetRemaining: LoopStatusInfo.LoopBudgetRemaining?
            if let brJSON = event.data["budget_remaining"],
               let brData = brJSON.data(using: .utf8),
               let br = try? JSONSerialization.jsonObject(with: brData) as? [String: Any] {
                budgetRemaining = LoopStatusInfo.LoopBudgetRemaining(
                    attempts: br["attempts"] as? Int,
                    commands: br["commands"] as? Int,
                    wallSeconds: br["wall_seconds"] as? Double
                )
            }

            messages[messageIndex].loopStatus = LoopStatusInfo(
                loopId: loopId,
                status: status,
                stopReason: stopReason,
                attempt: attempt,
                budgetRemaining: budgetRemaining,
                task: task
            )

        case "loop_attempt":
            if let attemptStr = event.data["attempt_number"],
               let attemptNum = Int(attemptStr) {
                if let ls = messages[messageIndex].loopStatus {
                    messages[messageIndex].loopStatus = LoopStatusInfo(
                        loopId: ls.loopId,
                        status: "running",
                        stopReason: nil,
                        attempt: attemptNum,
                        budgetRemaining: ls.budgetRemaining,
                        task: ls.task
                    )
                }
            }

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

    func refreshDiagnosticsSurface() async {
        do {
            systemStatus = try await api.fetchSystemStatus()
            promptDiagnostics = try? await api.fetchPromptDiagnostics(mode: selectedMode.rawValue)
            deployStatus = try? await api.fetchDeployStatus()
            diagnosticsRefreshError = nil
        } catch {
            diagnosticsRefreshError = "Diagnostics refresh failed. \(errorMessage(error))"
        }
    }

    func refreshReviewSurface() async {
        let requestedProjectPath = selectedReviewProjectPath ?? selectedProjectPath
        var failures: [String] = []

        do {
            let capabilities = try await api.fetchReviewCapabilities(projectPath: requestedProjectPath)
            reviewCapabilities = capabilities
            if selectedReviewProjectPath == nil || selectedReviewProjectPath?.isEmpty == true {
                selectedReviewProjectPath = capabilities.projectPath
            }
            if selectedReviewTarget != .auto,
                      !capabilities.availableTargets.contains(selectedReviewTarget.rawValue),
                      let fallback = ReviewTargetKind(rawValue: capabilities.defaultTarget) {
                selectedReviewTarget = fallback
            }
        } catch {
            failures.append("Review capabilities unavailable: \(errorMessage(error))")
        }

        do {
            reviewHistory = try await api.fetchReviewHistory(limit: 30)
            if let current = selectedReviewRun,
               let refreshed = reviewHistory.first(where: { $0.reviewId == current.reviewId }) {
                selectedReviewRun = refreshed
            } else if selectedReviewRun == nil {
                selectedReviewRun = reviewHistory.first
            }
        } catch {
            failures.append("Review history unavailable: \(errorMessage(error))")
        }

        if failures.isEmpty {
            reviewRefreshError = nil
        } else {
            reviewRefreshError = failures.joined(separator: "  ")
        }
    }

    func refreshJobsSurface() async {
        do {
            jobs = try await api.fetchJobs(limit: 80)
            if let current = selectedJob,
               let refreshed = jobs.first(where: { $0.jobId == current.jobId }) {
                selectedJob = refreshed
                selectedJobLog = try? await api.fetchJobLog(jobId: refreshed.jobId, limit: 240)
            } else if selectedJob == nil {
                selectedJob = jobs.first
                if let first = selectedJob {
                    selectedJobLog = try? await api.fetchJobLog(jobId: first.jobId, limit: 240)
                } else {
                    selectedJobLog = nil
                }
            }
            jobsRefreshError = nil
        } catch {
            jobsRefreshError = "Jobs refresh failed. \(errorMessage(error))"
        }
    }

    func refreshWorkersSurface() async {
        do {
            workPlans = try await api.fetchWorkPlans(limit: 50)
            if let current = selectedWorkPlan,
               let refreshed = workPlans.first(where: { $0.planId == current.planId }) {
                selectedWorkPlan = refreshed
            } else if selectedWorkPlan == nil {
                selectedWorkPlan = workPlans.first
            }
            workersRefreshError = nil
        } catch {
            workersRefreshError = "Workers refresh failed. \(errorMessage(error))"
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

    func showDiagnostics() {
        selectedSurface = .diagnostics
        Task { await refreshDiagnosticsSurface() }
    }

    func showJobs() {
        selectedSurface = .jobs
        Task { await refreshJobsSurface() }
    }

    func showPermissions() {
        selectedSurface = .permissions
        Task { await refreshPermissions() }
    }

    func showReview(projectPath: String? = nil) {
        if let projectPath {
            selectedReviewProjectPath = projectPath
        } else if selectedReviewProjectPath == nil {
            selectedReviewProjectPath = selectedProjectPath
        }
        selectedSurface = .review
        Task { await refreshReviewSurface() }
    }

    func showPreview() {
        selectedSurface = .preview
        Task { await refreshPreviewSurface() }
    }

    func showWorkers() {
        selectedSurface = .workers
        Task { await refreshWorkersSurface() }
    }

    func showDeploy() {
        selectedSurface = .deploy
        Task { await refreshDeploySurface() }
    }

    func refreshPreviewSurface() async {
        do {
            previewStatus = try await api.fetchPreviewStatus()
            previewRefreshError = nil
        } catch {
            previewRefreshError = "Preview refresh failed. \(errorMessage(error))"
        }
    }

    func refreshDeploySurface() async {
        do {
            deployStatus = try await api.fetchDeployStatus()
            deployments = try await api.fetchDeployments(limit: 50)
            if let current = selectedDeployment,
               let refreshed = deployments.first(where: { $0.deploymentId == current.deploymentId }) {
                selectedDeployment = refreshed
            } else if selectedDeployment == nil {
                selectedDeployment = deployments.first
            }
            deployRefreshError = nil
        } catch {
            deployRefreshError = "Deploy refresh failed. \(errorMessage(error))"
        }
    }

    func selectReviewTarget(_ target: ReviewTargetKind) {
        selectedReviewTarget = target
    }

    func selectReviewRun(_ run: ReviewRunInfo) {
        selectedReviewRun = run
    }

    func selectJob(_ job: BackgroundJobInfo) {
        selectedJob = job
        Task {
            do {
                selectedJobLog = try await api.fetchJobLog(jobId: job.jobId, limit: 240)
                jobsRefreshError = nil
            } catch {
                jobsRefreshError = "Couldn't load job log. \(errorMessage(error))"
            }
        }
    }

    func launchBackgroundJob() {
        let text = inputText.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachments = draftAttachments
        let requestText = requestMessage(text: text, attachments: attachments)
        guard !requestText.isEmpty, !isLaunchingBackgroundJob else { return }

        isLaunchingBackgroundJob = true
        jobsRefreshError = nil

        let jobSessionId = sessionId
        let mode = selectedMode
        let projectPath = selectedProjectPath
        let execStyle = selectedExecutionStyle

        Task {
            guard await ensureBackendReadyForBackgroundAction() else {
                isLaunchingBackgroundJob = false
                return
            }

            do {
                let job = try await api.launchBackgroundJob(
                    message: requestText,
                    sessionId: jobSessionId,
                    mode: mode,
                    projectPath: projectPath,
                    executionStyle: execStyle
                )
                inputText = ""
                draftAttachments = []
                selectedSurface = .jobs
                jobs.removeAll { $0.jobId == job.jobId }
                jobs.insert(job, at: 0)
                selectedJob = job
                selectedJobLog = try? await api.fetchJobLog(jobId: job.jobId, limit: 240)
                jobsRefreshError = nil
                await refreshJobsSurface()
            } catch {
                jobsRefreshError = "Couldn't launch background job. \(errorMessage(error))"
            }
            isLaunchingBackgroundJob = false
        }
    }

    func cancelJob(_ job: BackgroundJobInfo) {
        Task {
            do {
                let updated = try await api.cancelJob(jobId: job.jobId)
                replaceJob(updated)
                selectedJob = updated
                selectedJobLog = try? await api.fetchJobLog(jobId: updated.jobId, limit: 240)
                jobsRefreshError = nil
            } catch {
                jobsRefreshError = "Couldn't cancel background job. \(errorMessage(error))"
            }
        }
    }

    func resumeJob(_ job: BackgroundJobInfo) {
        Task {
            do {
                let updated = try await api.resumeJob(jobId: job.jobId)
                replaceJob(updated)
                selectedJob = updated
                selectedJobLog = try? await api.fetchJobLog(jobId: updated.jobId, limit: 240)
                jobsRefreshError = nil
            } catch {
                jobsRefreshError = "Couldn't resume background job. \(errorMessage(error))"
            }
        }
    }

    func takeOverJob(_ job: BackgroundJobInfo) {
        Task {
            do {
                let takeover = try await api.takeOverJob(jobId: job.jobId)
                applyJobTakeover(takeover)
                jobsRefreshError = nil
                await refreshJobsSurface()
            } catch {
                jobsRefreshError = "Couldn't take over background job. \(errorMessage(error))"
            }
        }
    }

    func runReview() {
        guard !isRunningReview else { return }
        isRunningReview = true
        reviewRefreshError = nil
        selectedSurface = .review

        let target = selectedReviewTarget
        let projectPath = normalizedReviewValue(selectedReviewProjectPath)
        let baseRef = normalizedReviewValue(reviewBaseRef)
        let headRef = normalizedReviewValue(reviewHeadRef)
        let filePaths = parsedReviewFilePaths()

        Task {
            do {
                let result = try await api.runReview(
                    target: target,
                    projectPath: projectPath,
                    baseRef: baseRef,
                    headRef: headRef,
                    filePaths: filePaths
                )
                reviewHistory.removeAll { $0.reviewId == result.reviewId }
                reviewHistory.insert(result, at: 0)
                selectedReviewRun = result
                reviewRefreshError = nil
                await refreshReviewSurface()
            } catch {
                reviewRefreshError = "Review failed. \(errorMessage(error))"
            }
            isRunningReview = false
        }
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

    func saveMemoryCandidate(candidateId: Int, label: String, text: String, evidence: String?) {
        Task {
            do {
                try await api.updateMemoryCandidate(candidateId: candidateId, label: label, text: text, evidence: evidence)
                memoryRefreshError = nil
                await refreshSidebar()
            } catch {
                memoryRefreshError = "Couldn't update pending memory. \(errorMessage(error))"
            }
        }
    }

    func approveMemoryCandidate(
        candidateId: Int,
        label: String,
        text: String,
        evidence: String?,
        pin: Bool = false
    ) {
        Task {
            do {
                try await api.approveMemoryCandidate(
                    candidateId: candidateId,
                    label: label,
                    text: text,
                    evidence: evidence,
                    pin: pin
                )
                memoryRefreshError = nil
                await refreshSidebar()
            } catch {
                memoryRefreshError = "Couldn't approve memory. \(errorMessage(error))"
            }
        }
    }

    func rejectMemoryCandidate(candidateId: Int) {
        Task {
            do {
                try await api.rejectMemoryCandidate(candidateId: candidateId)
                memoryRefreshError = nil
                await refreshSidebar()
            } catch {
                memoryRefreshError = "Couldn't reject memory. \(errorMessage(error))"
            }
        }
    }

    func expireMemoryCandidate(candidateId: Int) {
        Task {
            do {
                try await api.expireMemoryCandidate(candidateId: candidateId)
                memoryRefreshError = nil
                await refreshSidebar()
            } catch {
                memoryRefreshError = "Couldn't expire memory. \(errorMessage(error))"
            }
        }
    }

    func setMemoryPinned(itemId: Int, pinned: Bool) {
        Task {
            do {
                try await api.setMemoryPinned(itemId: itemId, pinned: pinned)
                memoryRefreshError = nil
                await refreshSidebar()
            } catch {
                memoryRefreshError = "Couldn't update pin state. \(errorMessage(error))"
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
        draftAttachments = []
        selectedProjectPath = nil
        selectedReviewProjectPath = nil
        memoryOverview = nil
        reviewCapabilities = nil
        reviewHistory = []
        selectedReviewRun = nil
        sidebarRefreshError = nil
        memoryRefreshError = nil
        diagnosticsRefreshError = nil
        reviewRefreshError = nil
        permissionsRefreshError = nil
        selectedSurface = .chat
        Task { await refreshSidebar() }
    }

    func selectMode(_ mode: WorkMode) {
        selectedMode = mode
    }

    private func requestMessage(text: String, attachments: [AttachmentItem]) -> String {
        let trimmedText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let attachmentContext = serializedAttachmentContext(attachments)

        if trimmedText.isEmpty {
            return attachmentContext
        }
        if attachmentContext.isEmpty {
            return trimmedText
        }
        return "\(trimmedText)\n\n\(attachmentContext)"
    }

    private func serializedAttachmentContext(_ attachments: [AttachmentItem]) -> String {
        guard !attachments.isEmpty else {
            return ""
        }

        var lines = ["Attached local files:"]
        var previewBudget = 24_000

        for attachment in attachments {
            lines.append("- \(attachment.displayName): \(attachment.path)")

            guard attachment.isPreviewableText,
                  previewBudget > 0,
                  let preview = textPreview(for: attachment, maxCharacters: min(8_000, previewBudget)) else {
                continue
            }

            lines.append("Contents of \(attachment.displayName):")
            lines.append("```text")
            lines.append(preview)
            lines.append("```")
            previewBudget -= preview.count
        }

        return lines.joined(separator: "\n")
    }

    private func textPreview(for attachment: AttachmentItem, maxCharacters: Int) -> String? {
        guard maxCharacters > 0 else {
            return nil
        }

        guard let values = try? attachment.url.resourceValues(forKeys: [.fileSizeKey]),
              let fileSize = values.fileSize,
              fileSize > 0,
              fileSize <= 16_384 else {
            return nil
        }

        guard let data = try? Data(contentsOf: attachment.url, options: .mappedIfSafe),
              !data.contains(0),
              let text = String(data: data, encoding: .utf8) ?? String(data: data, encoding: .ascii) else {
            return nil
        }

        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }

        if trimmed.count <= maxCharacters {
            return trimmed
        }
        return String(trimmed.prefix(maxCharacters)).trimmingCharacters(in: .whitespacesAndNewlines) + "\n...[truncated]"
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
        await refreshDiagnosticsSurface()
        await refreshPermissions()
    }

    private func ensureBackendReadyForBackgroundAction() async -> Bool {
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
            return false
        }
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

    private func applyJobTakeover(_ takeover: BackgroundJobTakeoverInfo) {
        sessionId = takeover.sessionId
        selectedMode = WorkMode(rawValue: takeover.mode) ?? .default
        selectedProjectPath = takeover.projectPath
        messages = takeover.messages.map(chatMessage(from:))
        if messages.isEmpty {
            messages = [ChatMessage(role: .system, content: "Background job ready in foreground chat.")]
        }
        selectedSurface = .chat
        isLoading = false
        activeToolName = nil
        pendingPermissionCount = 0
        currentAgent = AgentInfo.entryAgentName
    }

    private func replaceJob(_ updated: BackgroundJobInfo) {
        if let index = jobs.firstIndex(where: { $0.jobId == updated.jobId }) {
            jobs[index] = updated
        } else {
            jobs.insert(updated, at: 0)
        }
    }

    private func chatMessage(from info: SessionMessageInfo) -> ChatMessage {
        let role: ChatMessage.Role
        switch info.role.lowercased() {
        case "user":
            role = .user
        case "assistant":
            role = .assistant
        case "error":
            role = .error
        default:
            role = .system
        }
        return ChatMessage(role: role, content: info.content)
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

    private func parsedReviewFilePaths() -> [String] {
        reviewFilePathsText
            .split(whereSeparator: { $0 == "\n" || $0 == "," })
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    private func normalizedReviewValue(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}
