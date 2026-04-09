import SwiftUI

// MARK: - Color System (Strict Tokens)

extension Color {
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        let scanner = Scanner(string: hex)
        var rgb: UInt64 = 0
        scanner.scanHexInt64(&rgb)
        self.init(
            red: Double((rgb >> 16) & 0xFF) / 255,
            green: Double((rgb >> 8) & 0xFF) / 255,
            blue: Double(rgb & 0xFF) / 255
        )
    }
}

enum BossColor {
    // Base
    static let black      = Color(hex: "#000000")
    static let surface    = Color(hex: "#0A0A0A")
    static let surface2   = Color(hex: "#121212")

    // Text
    static let textPrimary   = Color(hex: "#EDEDED")
    static let textSecondary = Color(hex: "#A1A1AA")

    // Divider
    static let divider = Color.white.opacity(0.06)

    // Red accent — tight, controlled
    static let accent     = Color(hex: "#FF3B30")
    static let accentSoft = Color(hex: "#FF453A").opacity(0.18)
}

enum AppSurface: Equatable {
    case chat
    case memory
    case diagnostics
    case jobs
    case review
    case permissions
    case preview
    case workers
    case deploy
}

enum WorkMode: String, CaseIterable, Codable, Equatable {
    case ask
    case plan
    case agent
    case review

    static let `default`: WorkMode = .agent

    var label: String {
        switch self {
        case .ask: return "Ask"
        case .plan: return "Plan"
        case .agent: return "Agent"
        case .review: return "Review"
        }
    }

    var detail: String {
        switch self {
        case .ask:
            return "Read/search only"
        case .plan:
            return "Structured plan only"
        case .agent:
            return "Full governed actions"
        case .review:
            return "Findings-first review"
        }
    }
}

enum ReviewTargetKind: String, CaseIterable, Codable, Equatable {
    case auto
    case workingTree = "working_tree"
    case staged
    case branchDiff = "branch_diff"
    case files
    case projectSummary = "project_summary"

    var label: String {
        switch self {
        case .auto: return "Auto"
        case .workingTree: return "Working Tree"
        case .staged: return "Staged"
        case .branchDiff: return "Branch Diff"
        case .files: return "Files"
        case .projectSummary: return "Project Summary"
        }
    }

    var detail: String {
        switch self {
        case .auto:
            return "Pick the best local evidence"
        case .workingTree:
            return "Review current local changes"
        case .staged:
            return "Review staged diff only"
        case .branchDiff:
            return "Compare two refs"
        case .files:
            return "Review selected local files"
        case .projectSummary:
            return "Review indexed project context"
        }
    }
}

// MARK: - Data Models

struct ChatMessage: Identifiable, Equatable {
    let id = UUID()
    let role: Role
    var content: String
    var agent: String?
    var isStreaming: Bool = false
    var executionSteps: [ExecutionStep] = []
    var thinkingContent: String?
    var attachments: [AttachmentItem] = []
    var loopStatus: LoopStatusInfo?
    let timestamp = Date()

    enum Role: Equatable {
        case user
        case assistant
        case system
        case error
    }

    static func == (lhs: ChatMessage, rhs: ChatMessage) -> Bool {
        lhs.id == rhs.id &&
        lhs.content == rhs.content &&
        lhs.isStreaming == rhs.isStreaming &&
        lhs.agent == rhs.agent &&
        lhs.executionSteps == rhs.executionSteps &&
        lhs.thinkingContent == rhs.thinkingContent &&
        lhs.attachments == rhs.attachments &&
        lhs.loopStatus == rhs.loopStatus
    }
}

struct AttachmentItem: Identifiable, Equatable {
    let id: UUID
    let url: URL

    init(id: UUID = UUID(), url: URL) {
        self.id = id
        self.url = url.standardizedFileURL
    }

    var displayName: String {
        url.lastPathComponent
    }

    var path: String {
        url.path
    }

    var isImage: Bool {
        let ext = url.pathExtension.lowercased()
        return ["png", "jpg", "jpeg", "gif", "webp", "heic", "bmp", "tiff", "svg"].contains(ext)
    }

    var isPreviewableText: Bool {
        let ext = url.pathExtension.lowercased()
        if isImage {
            return false
        }
        return [
            "txt", "md", "markdown", "py", "swift", "json", "toml", "yaml", "yml",
            "js", "ts", "tsx", "jsx", "rb", "go", "rs", "java", "c", "cpp", "h",
            "hpp", "sh", "zsh", "bash", "xml", "html", "css", "sql"
        ].contains(ext)
    }

    var symbolName: String {
        if isImage {
            return "photo"
        }
        if isPreviewableText {
            return "doc.text"
        }
        return "doc"
    }
}

enum ExecutionType: String, Codable, Equatable {
    case read
    case search
    case plan
    case edit
    case run
    case external

    var requiresPermission: Bool {
        switch self {
        case .edit, .run, .external:
            return true
        case .read, .search, .plan:
            return false
        }
    }
}

enum ToolState: String, Equatable {
    case pending
    case waitingPermission
    case running
    case success
    case failure
}

enum PermissionDecision: String, Encodable, Equatable {
    case allowOnce = "allow_once"
    case alwaysAllow = "always_allow"
    case deny
}

struct PermissionRequest: Identifiable, Equatable {
    var id: String { approvalId }
    let runId: String
    let approvalId: String
    let name: String
    let title: String
    let description: String
    let executionType: ExecutionType
    let scopeLabel: String
}

struct ExecutionStep: Identifiable, Equatable {
    enum Kind: Equatable {
        case tool
        case handoff
    }

    let id: String
    let kind: Kind
    let name: String
    var title: String
    var description: String
    var arguments: String = ""
    var output: String?
    var state: ToolState = .pending
    var executionType: ExecutionType?
    var permissionRequest: PermissionRequest?
    var decision: PermissionDecision?

    static func == (lhs: ExecutionStep, rhs: ExecutionStep) -> Bool {
        lhs.id == rhs.id &&
        lhs.kind == rhs.kind &&
        lhs.name == rhs.name &&
        lhs.title == rhs.title &&
        lhs.description == rhs.description &&
        lhs.arguments == rhs.arguments &&
        lhs.output == rhs.output &&
        lhs.state == rhs.state &&
        lhs.executionType == rhs.executionType &&
        lhs.permissionRequest == rhs.permissionRequest &&
        lhs.decision == rhs.decision
    }
}

struct ProjectInfo: Identifiable, Decodable {
    let id: Int
    let path: String
    let name: String
    let type: String
    let git_remote: String?
    let git_branch: String?
    let metadata: [String: AnyCodable]?
}

struct FactInfo: Identifiable, Decodable {
    let id: Int
    let category: String
    let key: String
    let value: String
    let source: String
}

struct PermissionEntry: Identifiable, Decodable, Equatable {
    enum Decision: String, Decodable, Equatable {
        case allow
        case deny

        var label: String {
            switch self {
            case .allow: return "Allowed"
            case .deny: return "Denied"
            }
        }
    }

    let tool: String
    let scopeKey: String
    let scopeLabel: String
    let decision: Decision
    let executionType: ExecutionType
    let lastUsedAt: Date?
    let updatedAt: Date?

    var id: String { "\(tool):\(scopeKey)" }

    var rowTitle: String {
        "\(tool) · \(scopeLabel)"
    }

    enum CodingKeys: String, CodingKey {
        case tool
        case scopeKey = "scope_key"
        case scopeLabel = "scope_label"
        case decision
        case executionType = "execution_type"
        case lastUsedAt = "last_used_at"
        case updatedAt = "updated_at"
    }
}

struct MemoryStats: Decodable {
    let facts: Int
    let projects: Int
    let files_indexed: Int
    let last_project_scan_at: Date?
    let durable_memories: Int?
    let memory_candidates: Int?
    let pending_memory_candidates: Int?
    let pinned_durable_memories: Int?
    let conversation_episodes: Int?
    let project_notes: Int?
    let file_chunks: Int?
    let memory_types: [String: Int]?
    let memory_categories: [String: Int]?
}

struct MemoryRecord: Identifiable, Decodable {
    let sourceTable: String
    let memoryId: Int
    let memoryKind: String
    let category: String
    let label: String
    let text: String
    let source: String
    let projectPath: String?
    let updatedAt: Date?
    let lastUsedAt: Date?
    let confidence: Double
    let salience: Double
    let tags: [String]
    let deletable: Bool
    let pinned: Bool

    var id: String { "\(sourceTable):\(memoryId)" }

    enum CodingKeys: String, CodingKey {
        case sourceTable = "source_table"
        case memoryId = "memory_id"
        case memoryKind = "memory_kind"
        case category
        case label
        case text
        case source
        case projectPath = "project_path"
        case updatedAt = "updated_at"
        case lastUsedAt = "last_used_at"
        case confidence
        case salience
        case tags
        case deletable
        case pinned
    }
}

struct MemoryGovernanceInfo: Decodable {
    let pendingCandidates: Int
    let pinnedMemories: Int
    let autoApproveEnabled: Bool
    let autoApproveMinConfidence: Double

    enum CodingKeys: String, CodingKey {
        case pendingCandidates = "pending_candidates"
        case pinnedMemories = "pinned_memories"
        case autoApproveEnabled = "auto_approve_enabled"
        case autoApproveMinConfidence = "auto_approve_min_confidence"
    }
}

struct MemoryCandidateInfo: Identifiable, Decodable {
    let candidateId: Int
    let status: String
    let memoryKind: String
    let category: String
    let label: String
    let text: String
    let evidence: String
    let source: String
    let projectPath: String?
    let sessionId: String?
    let createdAt: Date?
    let updatedAt: Date?
    let expiresAt: Date?
    let confidence: Double
    let salience: Double
    let tags: [String]
    let existingMemoryId: Int?
    let promotedMemoryId: Int?
    let proposedAction: String
    let existingLabel: String?
    let existingText: String?

    var id: String { "candidate:\(candidateId)" }

    enum CodingKeys: String, CodingKey {
        case candidateId = "candidate_id"
        case status
        case memoryKind = "memory_kind"
        case category
        case label
        case text
        case evidence
        case source
        case projectPath = "project_path"
        case sessionId = "session_id"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case expiresAt = "expires_at"
        case confidence
        case salience
        case tags
        case existingMemoryId = "existing_memory_id"
        case promotedMemoryId = "promoted_memory_id"
        case proposedAction = "proposed_action"
        case existingLabel = "existing_label"
        case existingText = "existing_text"
    }
}

struct ProjectSummaryInfo: Identifiable, Decodable {
    let sourceTable: String
    let memoryId: Int
    let projectId: Int
    let projectPath: String
    let projectName: String
    let projectType: String
    let gitRemote: String?
    let gitBranch: String?
    let lastScanned: Date?
    let summaryTitle: String
    let summaryText: String
    let noteKey: String
    let memoryKind: String
    let updatedAt: Date?
    let source: String
    let metadata: [String: AnyCodable]?
    let deletable: Bool

    var id: String { "\(sourceTable):\(memoryId)" }

    enum CodingKeys: String, CodingKey {
        case sourceTable = "source_table"
        case memoryId = "memory_id"
        case projectId = "project_id"
        case projectPath = "project_path"
        case projectName = "project_name"
        case projectType = "project_type"
        case gitRemote = "git_remote"
        case gitBranch = "git_branch"
        case lastScanned = "last_scanned"
        case summaryTitle = "summary_title"
        case summaryText = "summary_text"
        case noteKey = "note_key"
        case memoryKind = "memory_kind"
        case updatedAt = "updated_at"
        case source
        case metadata
        case deletable
    }
}

struct ScanStatusInfo: Decodable {
    let lastScanAt: Date?
    let projectsIndexed: Int
    let filesIndexed: Int
    let durableMemories: Int
    let conversationEpisodes: Int
    let projectNotes: Int
    let fileChunks: Int

    enum CodingKeys: String, CodingKey {
        case lastScanAt = "last_scan_at"
        case projectsIndexed = "projects_indexed"
        case filesIndexed = "files_indexed"
        case durableMemories = "durable_memories"
        case conversationEpisodes = "conversation_episodes"
        case projectNotes = "project_notes"
        case fileChunks = "file_chunks"
    }
}

struct MemoryInjectionReason: Identifiable, Decodable {
    let sourceTable: String
    let memoryId: Int
    let memoryKind: String
    let category: String
    let key: String
    let text: String
    let projectPath: String?
    let score: Double
    let why: String
    let deletable: Bool
    let reviewState: String
    let pinned: Bool

    var id: String { "\(sourceTable):\(memoryId)" }

    enum CodingKeys: String, CodingKey {
        case sourceTable = "source_table"
        case memoryId = "memory_id"
        case memoryKind = "memory_kind"
        case category
        case key
        case text
        case projectPath = "project_path"
        case score
        case why
        case deletable
        case reviewState = "review_state"
        case pinned
    }
}

struct MemoryInjectionInfo: Decodable {
    let message: String
    let query: String
    let projectPath: String?
    let text: String
    let reasons: [MemoryInjectionReason]

    enum CodingKeys: String, CodingKey {
        case message
        case query
        case projectPath = "project_path"
        case text
        case reasons
    }
}

struct MemoryOverview: Decodable {
    let userProfile: [MemoryRecord]
    let preferences: [MemoryRecord]
    let recentMemories: [MemoryRecord]
    let pendingCandidates: [MemoryCandidateInfo]
    let governance: MemoryGovernanceInfo
    let conversationSummaries: [MemoryRecord]
    let projectSummaries: [ProjectSummaryInfo]
    let scanStatus: ScanStatusInfo
    let currentTurnMemory: MemoryInjectionInfo?

    enum CodingKeys: String, CodingKey {
        case userProfile = "user_profile"
        case preferences
        case recentMemories = "recent_memories"
        case pendingCandidates = "pending_candidates"
        case governance
        case conversationSummaries = "conversation_summaries"
        case projectSummaries = "project_summaries"
        case scanStatus = "scan_status"
        case currentTurnMemory = "current_turn_memory"
    }
}

struct ReviewCapabilitiesInfo: Decodable {
    let workspaceRoot: String
    let projectPath: String
    let repoRoot: String?
    let gitAvailable: Bool
    let currentBranch: String?
    let workingTreeFiles: [String]
    let stagedFiles: [String]
    let hasWorkingTreeChanges: Bool
    let hasStagedChanges: Bool
    let indexedProjectAvailable: Bool
    let availableTargets: [String]
    let defaultTarget: String

    enum CodingKeys: String, CodingKey {
        case workspaceRoot = "workspace_root"
        case projectPath = "project_path"
        case repoRoot = "repo_root"
        case gitAvailable = "git_available"
        case currentBranch = "current_branch"
        case workingTreeFiles = "working_tree_files"
        case stagedFiles = "staged_files"
        case hasWorkingTreeChanges = "has_working_tree_changes"
        case hasStagedChanges = "has_staged_changes"
        case indexedProjectAvailable = "indexed_project_available"
        case availableTargets = "available_targets"
        case defaultTarget = "default_target"
    }
}

struct ReviewFindingInfo: Identifiable, Decodable, Equatable {
    let severity: String
    let filePath: String
    let evidence: String
    let risk: String
    let recommendedFix: String

    var id: String { "\(severity):\(filePath):\(evidence)" }

    enum CodingKeys: String, CodingKey {
        case severity
        case filePath = "file_path"
        case evidence
        case risk
        case recommendedFix = "recommended_fix"
    }
}

struct ReviewRunInfo: Identifiable, Decodable, Equatable {
    let reviewId: String
    let createdAt: Date?
    let title: String
    let targetKind: String
    let targetLabel: String
    let scopeSummary: String
    let projectPath: String?
    let repoRoot: String?
    let baseRef: String?
    let headRef: String?
    let filePaths: [String]
    let summary: String
    let residualRisk: String
    let findings: [ReviewFindingInfo]
    let severityCounts: [String: Int]

    var id: String { reviewId }

    enum CodingKeys: String, CodingKey {
        case reviewId = "review_id"
        case createdAt = "created_at"
        case title
        case targetKind = "target_kind"
        case targetLabel = "target_label"
        case scopeSummary = "scope_summary"
        case projectPath = "project_path"
        case repoRoot = "repo_root"
        case baseRef = "base_ref"
        case headRef = "head_ref"
        case filePaths = "file_paths"
        case summary
        case residualRisk = "residual_risk"
        case findings
        case severityCounts = "severity_counts"
    }
}

struct BackgroundJobApprovalInfo: Identifiable, Decodable, Equatable {
    let approvalId: String
    let toolName: String
    let title: String
    let description: String
    let executionType: String
    let scopeLabel: String
    let requestedAt: Date?
    let expiresAt: Date?
    let status: String

    var id: String { approvalId }

    enum CodingKeys: String, CodingKey {
        case approvalId = "approval_id"
        case toolName = "tool_name"
        case title
        case description
        case executionType = "execution_type"
        case scopeLabel = "scope_label"
        case requestedAt = "requested_at"
        case expiresAt = "expires_at"
        case status
    }
}

struct BackgroundJobInfo: Identifiable, Decodable, Equatable {
    let jobId: String
    let title: String
    let prompt: String
    let mode: String
    let sessionId: String
    let projectPath: String?
    let status: String
    let createdAt: Date?
    let updatedAt: Date?
    let startedAt: Date?
    let finishedAt: Date?
    let lastEventAt: Date?
    let latestEvent: String?
    let logPath: String
    let errorMessage: String?
    let pendingRunId: String?
    let resumeCount: Int
    let sessionPersisted: Bool
    let assistantPreview: String
    let branchMode: String?
    let branchName: String?
    let taskSlug: String?
    let branchStatus: String?
    let branchMessage: String?
    let branchHelperPath: String?
    let active: Bool
    let approvals: [BackgroundJobApprovalInfo]

    var id: String { jobId }

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case title
        case prompt
        case mode
        case sessionId = "session_id"
        case projectPath = "project_path"
        case status
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case lastEventAt = "last_event_at"
        case latestEvent = "latest_event"
        case logPath = "log_path"
        case errorMessage = "error_message"
        case pendingRunId = "pending_run_id"
        case resumeCount = "resume_count"
        case sessionPersisted = "session_persisted"
        case assistantPreview = "assistant_preview"
        case branchMode = "branch_mode"
        case branchName = "branch_name"
        case taskSlug = "task_slug"
        case branchStatus = "branch_status"
        case branchMessage = "branch_message"
        case branchHelperPath = "branch_helper_path"
        case active
        case approvals
    }
}

struct BackgroundJobLogEntryInfo: Identifiable, Decodable, Equatable {
    let timestamp: String
    let type: String
    let message: String

    var id: String { "\(timestamp):\(type):\(message)" }
}

struct BackgroundJobLogTailInfo: Decodable, Equatable {
    let jobId: String
    let logPath: String
    let entries: [BackgroundJobLogEntryInfo]
    let text: String
    let truncated: Bool

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case logPath = "log_path"
        case entries
        case text
        case truncated
    }
}

struct SessionMessageInfo: Decodable, Equatable {
    let role: String
    let content: String
}

struct BackgroundJobTakeoverInfo: Decodable, Equatable {
    let job: BackgroundJobInfo
    let sessionId: String
    let mode: String
    let projectPath: String?
    let messages: [SessionMessageInfo]

    enum CodingKeys: String, CodingKey {
        case job
        case sessionId = "session_id"
        case mode
        case projectPath = "project_path"
        case messages
    }
}

struct RuntimeTrustInfo: Decodable {
    let lockExists: Bool?
    let lockPath: String?
    let lockStatus: String?
    let lockPid: Int?
    let lockPidAlive: Bool?
    let port: Int?
    let portInUse: Bool?
    let listenerPids: [Int]?
    let processCommand: String?
    let processCwd: String?
    let processExecutable: String?
    let reportedWorkspacePath: String?
    let reportedInterpreterPath: String?
    let warnings: [String]?

    enum CodingKeys: String, CodingKey {
        case lockExists = "lock_exists"
        case lockPath = "lock_path"
        case lockStatus = "lock_status"
        case lockPid = "lock_pid"
        case lockPidAlive = "lock_pid_alive"
        case port
        case portInUse = "port_in_use"
        case listenerPids = "listener_pids"
        case processCommand = "process_command"
        case processCwd = "process_cwd"
        case processExecutable = "process_executable"
        case reportedWorkspacePath = "reported_workspace_path"
        case reportedInterpreterPath = "reported_interpreter_path"
        case warnings
    }
}

struct GitStatusInfo: Decodable {
    let available: Bool?
    let isRepo: Bool?
    let repoRoot: String?
    let branch: String?
    let branchSummary: String?
    let clean: Bool?
    let stagedCount: Int?
    let unstagedCount: Int?
    let untrackedCount: Int?
    let changedFileCount: Int?
    let summary: String?

    enum CodingKeys: String, CodingKey {
        case available
        case isRepo = "is_repo"
        case repoRoot = "repo_root"
        case branch
        case branchSummary = "branch_summary"
        case clean
        case stagedCount = "staged_count"
        case unstagedCount = "unstaged_count"
        case untrackedCount = "untracked_count"
        case changedFileCount = "changed_file_count"
        case summary
    }
}

struct BossControlFileStatusInfo: Decodable, Equatable {
    let path: String
    let exists: Bool
}

struct BossControlRuleStatusInfo: Decodable, Equatable {
    let name: String?
    let title: String
    let path: String
}

struct BossControlStatusInfo: Decodable {
    let configured: Bool?
    let defaultMode: String?
    let reviewModeName: String?
    let files: [String: BossControlFileStatusInfo]?
    let rules: [BossControlRuleStatusInfo]?

    enum CodingKeys: String, CodingKey {
        case configured
        case defaultMode = "default_mode"
        case reviewModeName = "review_mode_name"
        case files
        case rules
    }
}

struct BossControlHealthInfo: Decodable {
    let configured: Bool?
    let healthy: Bool?
    let rulesCount: Int?
    let rulesHealthy: Bool?
    let missingFiles: [String]?
    let defaultMode: String?
    let reviewModeName: String?

    enum CodingKeys: String, CodingKey {
        case configured
        case healthy
        case rulesCount = "rules_count"
        case rulesHealthy = "rules_healthy"
        case missingFiles = "missing_files"
        case defaultMode = "default_mode"
        case reviewModeName = "review_mode_name"
    }
}

struct PromptLayerInfo: Decodable {
    let kind: String
    let source: String
    let active: Bool
    let contentLength: Int

    enum CodingKeys: String, CodingKey {
        case kind, source, active
        case contentLength = "content_length"
    }
}

struct PromptDiagnosticsInfo: Decodable {
    let mode: String
    let agentName: String
    let taskHint: String?
    let totalLayers: Int
    let activeLayers: Int
    let totalChars: Int
    let activeKinds: [String]?
    let instructionSources: [String]?
    let reviewGuidanceActive: Bool?
    let frontendGuidanceActive: Bool?
    let layers: [PromptLayerInfo]?

    enum CodingKeys: String, CodingKey {
        case mode
        case agentName = "agent_name"
        case taskHint = "task_hint"
        case totalLayers = "total_layers"
        case activeLayers = "active_layers"
        case totalChars = "total_chars"
        case activeKinds = "active_kinds"
        case instructionSources = "instruction_sources"
        case reviewGuidanceActive = "review_guidance_active"
        case frontendGuidanceActive = "frontend_guidance_active"
        case layers
    }
}

struct PreviewCapabilitiesInfo: Decodable {
    let hasBrowser: Bool
    let browserPath: String?
    let hasPlaywright: Bool
    let hasNode: Bool
    let hasSwiftBuild: Bool
    let policyEnforced: Bool?

    enum CodingKeys: String, CodingKey {
        case hasBrowser = "has_browser"
        case browserPath = "browser_path"
        case hasPlaywright = "has_playwright"
        case hasNode = "has_node"
        case hasSwiftBuild = "has_swift_build"
        case policyEnforced = "policy_enforced"
    }
}

struct PreviewCaptureInfo: Decodable {
    let screenshotPath: String?
    let domSummary: String?
    let consoleErrors: [String]?
    let networkErrors: [String]?
    let pageTitle: String?
    let timestamp: Double?
    let detailMode: String?
    let verificationMethod: String?
    let region: [String: Int]?
    let policyEnforced: Bool?

    enum CodingKeys: String, CodingKey {
        case screenshotPath = "screenshot_path"
        case domSummary = "dom_summary"
        case consoleErrors = "console_errors"
        case networkErrors = "network_errors"
        case pageTitle = "page_title"
        case timestamp
        case detailMode = "detail_mode"
        case verificationMethod = "verification_method"
        case region
        case policyEnforced = "policy_enforced"
    }
}

struct PreviewSessionInfo: Decodable {
    let sessionId: String
    let projectPath: String
    let url: String?
    let status: String
    let startCommand: String?
    let pid: Int?
    let startedAt: Double?
    let errorMessage: String?
    let lastCapture: PreviewCaptureInfo?
    let verificationMethod: String?
    let policyEnforced: Bool?

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case projectPath = "project_path"
        case url, status
        case startCommand = "start_command"
        case pid
        case startedAt = "started_at"
        case errorMessage = "error_message"
        case lastCapture = "last_capture"
        case verificationMethod = "verification_method"
        case policyEnforced = "policy_enforced"
    }
}

struct PreviewStatusInfo: Decodable {
    let capabilities: PreviewCapabilitiesInfo
    let sessions: [PreviewSessionInfo]
    let activeCount: Int
    let visionAvailable: Bool?

    enum CodingKeys: String, CodingKey {
        case capabilities, sessions
        case activeCount = "active_count"
        case visionAvailable = "vision_available"
    }
}

struct DiagnosticsSummaryInfo: Decodable {
    let providerMode: String?
    let gitAvailable: Bool?
    let gitSummary: String?
    let repoClean: Bool?
    let pendingMemoryCount: Int?
    let pendingJobsCount: Int?
    let pendingRunsCount: Int?
    let lockConsistent: Bool?
    let statusWarnings: [String]?
    let bossControlConfigured: Bool?
    let bossControlHealthy: Bool?
    let rulesCount: Int?

    enum CodingKeys: String, CodingKey {
        case providerMode = "provider_mode"
        case gitAvailable = "git_available"
        case gitSummary = "git_summary"
        case repoClean = "repo_clean"
        case pendingMemoryCount = "pending_memory_count"
        case pendingJobsCount = "pending_jobs_count"
        case pendingRunsCount = "pending_runs_count"
        case lockConsistent = "lock_consistent"
        case statusWarnings = "status_warnings"
        case bossControlConfigured = "boss_control_configured"
        case bossControlHealthy = "boss_control_healthy"
        case rulesCount = "rules_count"
    }
}

struct SystemStatusInfo: Decodable {
    let providerMode: String?
    let appVersion: String?
    let buildMarker: String?
    let processId: Int?
    let startedAt: Date?
    let readyAt: Date?
    let interpreterPath: String?
    let workspacePath: String?
    let currentWorkingDirectory: String?
    let git: GitStatusInfo?
    let runtimeTrust: RuntimeTrustInfo?
    let bossControl: BossControlStatusInfo?
    let bossControlHealth: BossControlHealthInfo?
    let diagnostics: DiagnosticsSummaryInfo?
    let providerRegistry: ProviderRegistryInfo?
    let pendingRunsCount: Int?
    let pendingApprovalsCount: Int?
    let stalePendingRunsCount: Int?
    let backgroundJobsCount: Int?

    enum CodingKeys: String, CodingKey {
        case providerMode = "provider_mode"
        case appVersion = "app_version"
        case buildMarker = "build_marker"
        case processId = "process_id"
        case startedAt = "started_at"
        case readyAt = "ready_at"
        case interpreterPath = "interpreter_path"
        case workspacePath = "workspace_path"
        case currentWorkingDirectory = "current_working_directory"
        case git
        case runtimeTrust = "runtime_trust"
        case bossControl = "boss_control"
        case bossControlHealth = "boss_control_health"
        case diagnostics
        case providerRegistry = "provider_registry"
        case pendingRunsCount = "pending_runs_count"
        case pendingApprovalsCount = "pending_approvals_count"
        case stalePendingRunsCount = "stale_pending_runs_count"
        case backgroundJobsCount = "background_jobs_count"
    }
}

// MARK: - Provider Registry

struct ProviderRegistryInfo: Decodable {
    let providers: [ProviderInfoItem]?
    let routing: [String: String]?
    let capabilityMap: [String: [String]]?

    enum CodingKeys: String, CodingKey {
        case providers
        case routing
        case capabilityMap = "capability_map"
    }
}

struct ProviderInfoItem: Decodable, Identifiable {
    var id: String { name }

    let name: String
    let kind: String
    let baseUrl: String?
    let capabilities: [String]?
    let models: [String]?
    let enabled: Bool?
    let health: ProviderHealthInfo?

    enum CodingKeys: String, CodingKey {
        case name, kind
        case baseUrl = "base_url"
        case capabilities, models, enabled, health
    }
}

struct ProviderHealthInfo: Decodable {
    let status: String
    let latencyMs: Double?
    let error: String?
    let checkedAt: Double?

    enum CodingKeys: String, CodingKey {
        case status
        case latencyMs = "latency_ms"
        case error
        case checkedAt = "checked_at"
    }
}

struct AnyCodable: Decodable {
    let value: Any

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let s = try? container.decode(String.self) { value = s }
        else if let i = try? container.decode(Int.self) { value = i }
        else if let d = try? container.decode(Double.self) { value = d }
        else if let b = try? container.decode(Bool.self) { value = b }
        else if let dict = try? container.decode([String: AnyCodable].self) { value = dict }
        else if let arr = try? container.decode([AnyCodable].self) { value = arr }
        else { value = "null" }
    }
}

// MARK: - Agent metadata

struct AgentInfo {
    let icon: String
    let display: String

    static let entryAgentName = "general"

    static func forName(_ name: String) -> AgentInfo {
        switch name {
        case entryAgentName: return AgentInfo(icon: "bubble.left", display: "General")
        case "mac":       return AgentInfo(icon: "desktopcomputer", display: "Mac")
        case "research":  return AgentInfo(icon: "magnifyingglass", display: "Research")
        case "reasoning": return AgentInfo(icon: "brain.head.profile", display: "Reasoning")
        case "code":      return AgentInfo(icon: "chevron.left.forwardslash.chevron.right", display: "Code")
        default:          return AgentInfo(icon: "questionmark.circle", display: name)
        }
    }
}

// MARK: - Loop

enum ExecutionStyle: String, CaseIterable, Codable, Equatable {
    case singlePass = "single_pass"
    case iterative = "iterative"

    var label: String {
        switch self {
        case .singlePass: return "Single Pass"
        case .iterative: return "Iterative Loop"
        }
    }

    var detail: String {
        switch self {
        case .singlePass: return "Standard one-shot response"
        case .iterative: return "Bounded edit-run-test-fix loop"
        }
    }
}

struct LoopStatusInfo: Equatable {
    let loopId: String
    let status: String
    let stopReason: String?
    let attempt: Int?
    let budgetRemaining: LoopBudgetRemaining?
    let task: String?

    struct LoopBudgetRemaining: Equatable {
        let attempts: Int?
        let commands: Int?
        let wallSeconds: Double?
    }
}

struct LoopAttemptInfo: Equatable {
    let loopId: String
    let attemptNumber: Int
    let phase: String
    let budgetRemaining: LoopStatusInfo.LoopBudgetRemaining?
}


// MARK: - Workers

enum WorkerRole: String, CaseIterable, Codable {
    case explorer
    case implementer
    case reviewer

    var label: String {
        switch self {
        case .explorer: return "Explorer"
        case .implementer: return "Implementer"
        case .reviewer: return "Reviewer"
        }
    }

    var detail: String {
        switch self {
        case .explorer: return "Read-only context gathering"
        case .implementer: return "Isolated coding work"
        case .reviewer: return "Read-only validation"
        }
    }
}

struct WorkerInfo: Identifiable, Decodable, Equatable {
    let workerId: String
    let planId: String
    let role: String
    let scope: String
    let fileTargets: [String]
    let state: String
    let workspaceId: String?
    let workspacePath: String?
    let startedAt: Double?
    let finishedAt: Double?
    let error: String?
    let resultSummary: String
    let outputArtifacts: [String]
    let logLines: [String]

    var id: String { workerId }

    enum CodingKeys: String, CodingKey {
        case workerId = "worker_id"
        case planId = "plan_id"
        case role
        case scope
        case fileTargets = "file_targets"
        case state
        case workspaceId = "workspace_id"
        case workspacePath = "workspace_path"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case error
        case resultSummary = "result_summary"
        case outputArtifacts = "output_artifacts"
        case logLines = "log_lines"
    }
}

struct WorkPlanInfo: Identifiable, Decodable, Equatable {
    let planId: String
    let task: String
    let projectPath: String
    let sessionId: String
    let status: String
    let workers: [WorkerInfo]
    let mergeStrategy: String
    let mergeSummary: String
    let createdAt: Double
    let updatedAt: Double
    let finishedAt: Double?
    let error: String?
    let maxConcurrent: Int

    var id: String { planId }

    enum CodingKeys: String, CodingKey {
        case planId = "plan_id"
        case task
        case projectPath = "project_path"
        case sessionId = "session_id"
        case status
        case workers
        case mergeStrategy = "merge_strategy"
        case mergeSummary = "merge_summary"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case finishedAt = "finished_at"
        case error
        case maxConcurrent = "max_concurrent"
    }
}

struct WorkPlanSummaryInfo: Decodable, Equatable {
    let planId: String
    let task: String
    let status: String
    let workerCount: Int
    let workersByState: [String: Int]
    let mergeStrategy: String
    let mergeSummary: String

    enum CodingKeys: String, CodingKey {
        case planId = "plan_id"
        case task
        case status
        case workerCount = "worker_count"
        case workersByState = "workers_by_state"
        case mergeStrategy = "merge_strategy"
        case mergeSummary = "merge_summary"
    }
}

struct ConflictValidationInfo: Decodable, Equatable {
    let fileConflicts: ConflictSection
    let directoryOverlap: ConflictSection

    struct ConflictSection: Decodable, Equatable {
        let hasConflicts: Bool
        let detail: String

        enum CodingKeys: String, CodingKey {
            case hasConflicts = "has_conflicts"
            case detail
        }
    }

    enum CodingKeys: String, CodingKey {
        case fileConflicts = "file_conflicts"
        case directoryOverlap = "directory_overlap"
    }
}

// MARK: - Deploy Models

struct DeployStatusInfo: Decodable, Equatable {
    let enabled: Bool
    let adapters: [DeployAdapterInfo]
    let configuredCount: Int
    let recentDeployments: Int
    let liveCount: Int

    enum CodingKeys: String, CodingKey {
        case enabled
        case adapters
        case configuredCount = "configured_count"
        case recentDeployments = "recent_deployments"
        case liveCount = "live_count"
    }
}

struct DeployAdapterInfo: Decodable, Equatable {
    let adapter: String
    let configured: Bool
}

struct DeploymentInfo: Identifiable, Decodable, Equatable {
    let deploymentId: String
    let projectPath: String
    let sessionId: String
    let adapter: String
    let target: String
    let status: String
    let previewUrl: String?
    let buildLog: String
    let deployLog: String
    let error: String?
    let createdAt: Double
    let updatedAt: Double
    let finishedAt: Double?

    var id: String { deploymentId }

    enum CodingKeys: String, CodingKey {
        case deploymentId = "deployment_id"
        case projectPath = "project_path"
        case sessionId = "session_id"
        case adapter
        case target
        case status
        case previewUrl = "preview_url"
        case buildLog = "build_log"
        case deployLog = "deploy_log"
        case error
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case finishedAt = "finished_at"
    }
}
