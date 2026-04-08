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
    case permissions
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
        lhs.thinkingContent == rhs.thinkingContent
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
    let conversationSummaries: [MemoryRecord]
    let projectSummaries: [ProjectSummaryInfo]
    let scanStatus: ScanStatusInfo
    let currentTurnMemory: MemoryInjectionInfo?

    enum CodingKeys: String, CodingKey {
        case userProfile = "user_profile"
        case preferences
        case recentMemories = "recent_memories"
        case conversationSummaries = "conversation_summaries"
        case projectSummaries = "project_summaries"
        case scanStatus = "scan_status"
        case currentTurnMemory = "current_turn_memory"
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
    let runtimeTrust: RuntimeTrustInfo?

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
        case runtimeTrust = "runtime_trust"
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
