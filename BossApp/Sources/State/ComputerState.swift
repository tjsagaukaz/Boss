import AppKit
import SwiftUI

// MARK: - Computer Session State

@MainActor
final class ComputerState: ObservableObject {
    @Published var session: ComputerSessionInfo?
    @Published var events: [ComputerEventInfo] = []
    @Published var capabilities: ComputerCapabilitiesInfo?
    @Published var refreshError: String?
    @Published var isActive: Bool = false
    @Published var screenshotImage: NSImage?
    @Published var actionInFlight: Bool = false

    /// The viewport size (pixels) used by the backend browser.  Must match the
    /// backend's create-session viewport_width / viewport_height so coordinate
    /// overlays land correctly on the aspect-fit screenshot.
    @Published var viewportSize: CGSize = .init(width: 1280, height: 800)

    private let api = APIClient.shared
    private var pollTask: Task<Void, Never>?

    // MARK: - Polling

    func startPolling(sessionId: String) {
        stopPolling()
        isActive = true
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh(sessionId: sessionId)
                try? await Task.sleep(nanoseconds: 1_500_000_000) // 1.5s
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func refresh(sessionId: String) async {
        do {
            let dict = try await api.fetchComputerSession(sessionId)
            let updated = ComputerSessionInfo.from(dict: dict)
            session = updated

            let eventsData = try await api.fetchComputerEvents(sessionId)
            events = eventsData.compactMap { ComputerEventInfo.from(dict: $0) }

            // Fetch screenshot
            if let _ = updated.latestScreenshotPath {
                do {
                    let imgData = try await api.fetchComputerScreenshot(sessionId)
                    if let img = NSImage(data: imgData) {
                        screenshotImage = img
                    }
                } catch {
                    // Screenshot may not be ready yet — ignore
                }
            }

            refreshError = nil

            // Stop polling when terminal
            if updated.status.isTerminal {
                stopPolling()
                isActive = false
            }
        } catch {
            refreshError = error.localizedDescription
        }
    }

    // MARK: - Actions

    func approve(decision: String) async {
        guard let session, let approvalId = session.pendingApprovalId else { return }
        actionInFlight = true
        defer { actionInFlight = false }
        do {
            let dict = try await api.computerApprove(
                sessionId: session.sessionId,
                approvalId: approvalId,
                decision: decision
            )
            self.session = ComputerSessionInfo.from(dict: dict)
        } catch {
            refreshError = error.localizedDescription
        }
    }

    func pause() async {
        guard let session, !session.status.isTerminal else { return }
        actionInFlight = true
        defer { actionInFlight = false }
        do {
            let _ = try await api.computerPause(sessionId: session.sessionId)
        } catch {
            refreshError = error.localizedDescription
        }
    }

    func resume() async {
        guard let session else { return }
        actionInFlight = true
        defer { actionInFlight = false }
        do {
            let dict = try await api.computerResume(sessionId: session.sessionId)
            self.session = ComputerSessionInfo.from(dict: dict)
        } catch {
            refreshError = error.localizedDescription
        }
    }

    func cancel() async {
        guard let session, !session.status.isTerminal else { return }
        actionInFlight = true
        defer { actionInFlight = false }
        do {
            let _ = try await api.computerCancel(sessionId: session.sessionId)
        } catch {
            refreshError = error.localizedDescription
        }
    }

    // Dummy data for layout testing — replaced when API endpoints ship
    func loadDummy() {
        let now = Date()
        session = ComputerSessionInfo(
            sessionId: "a1b2c3d4e5f6",
            targetUrl: "https://github.com/settings/tokens",
            targetDomain: "github.com",
            currentUrl: nil,
            currentDomain: nil,
            status: .running,
            browserStatus: .active,
            activeModel: "gpt-5.4",
            turnIndex: 7,
            latestScreenshotPath: nil,
            latestScreenshotTimestamp: now.addingTimeInterval(-3),
            lastActionBatch: [
                ComputerActionInfo(type: "click", x: 412, y: 308),
                ComputerActionInfo(type: "type", text: "boss-token"),
            ],
            lastActionResults: [
                ComputerActionResultInfo(actionType: "click", success: true),
                ComputerActionResultInfo(actionType: "type", success: true),
            ],
            createdAt: now.addingTimeInterval(-45),
            updatedAt: now.addingTimeInterval(-3),
            error: nil,
            approvalPending: false,
            pendingApprovalId: nil,
            domainAllowlisted: true,
            task: "Generate a new personal access token"
        )
        events = [
            ComputerEventInfo(timestamp: now.addingTimeInterval(-45), event: "session_created", detail: "Target: github.com/settings/tokens"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-42), event: "browser_launching", detail: nil),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-40), event: "browser_ready", detail: nil),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-39), event: "navigated", detail: "https://github.com/settings/tokens"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-35), event: "screenshot", detail: "Turn 1"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-30), event: "action_executed", detail: "click (412, 308)"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-28), event: "action_executed", detail: "type \"Generate new…\""),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-25), event: "turn_completed", detail: "Turn 1 — 2 actions, 0 failures"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-20), event: "screenshot", detail: "Turn 2"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-15), event: "action_executed", detail: "click (520, 440)"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-12), event: "turn_completed", detail: "Turn 2 — 1 action, 0 failures"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-8), event: "screenshot", detail: "Turn 7"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-5), event: "action_executed", detail: "click (412, 308)"),
            ComputerEventInfo(timestamp: now.addingTimeInterval(-3), event: "action_executed", detail: "type \"boss-token\""),
        ]
        capabilities = ComputerCapabilitiesInfo(
            playwrightInstalled: true,
            browsersInstalled: true,
            screenshotSupported: true,
            modelReady: true,
            model: "gpt-5.4",
            canRunSession: true
        )
        isActive = true
    }

    func clearSession() {
        stopPolling()
        session = nil
        events = []
        screenshotImage = nil
        isActive = false
    }
}

// MARK: - Data Models

enum ComputerSessionStatus: String, Codable {
    case created
    case launching
    case running
    case paused
    case waitingApproval = "waiting_approval"
    case completed
    case failed
    case cancelled

    var label: String {
        switch self {
        case .created: return "Created"
        case .launching: return "Launching"
        case .running: return "Running"
        case .paused: return "Paused"
        case .waitingApproval: return "Waiting Approval"
        case .completed: return "Completed"
        case .failed: return "Failed"
        case .cancelled: return "Cancelled"
        }
    }

    var isTerminal: Bool {
        self == .completed || self == .failed || self == .cancelled
    }

    var color: Color {
        switch self {
        case .running: return BossColor.accent
        case .paused, .waitingApproval: return Color(hex: "#FBBF24")
        case .completed: return Color(hex: "#34D399")
        case .failed, .cancelled: return Color(hex: "#F87171")
        case .created, .launching: return BossColor.textSecondary
        }
    }
}

enum ComputerBrowserStatus: String, Codable {
    case notStarted = "not_started"
    case launching
    case ready
    case navigating
    case active
    case closed
    case error
}

struct ComputerSessionInfo: Identifiable {
    var id: String { sessionId }
    let sessionId: String
    let targetUrl: String?
    let targetDomain: String?
    let currentUrl: String?
    let currentDomain: String?
    let status: ComputerSessionStatus
    let browserStatus: ComputerBrowserStatus
    let activeModel: String
    let turnIndex: Int
    let latestScreenshotPath: String?
    let latestScreenshotTimestamp: Date?
    let lastActionBatch: [ComputerActionInfo]
    let lastActionResults: [ComputerActionResultInfo]
    let createdAt: Date
    let updatedAt: Date
    let error: String?

    // Approval fields
    let approvalPending: Bool
    let pendingApprovalId: String?

    // Domain safety
    let domainAllowlisted: Bool

    // Task description
    let task: String?

    static func from(dict: [String: Any]) -> ComputerSessionInfo {
        let actions: [ComputerActionInfo] = (dict["last_action_batch"] as? [[String: Any]] ?? []).map { a in
            ComputerActionInfo(
                type: a["type"] as? String ?? "unknown",
                x: a["x"] as? Int,
                y: a["y"] as? Int,
                text: a["text"] as? String,
                key: a["key"] as? String,
                url: a["url"] as? String
            )
        }
        let results: [ComputerActionResultInfo] = (dict["last_action_results"] as? [[String: Any]] ?? []).map { r in
            ComputerActionResultInfo(
                actionType: r["action_type"] as? String ?? "unknown",
                success: r["success"] as? Bool ?? true,
                error: r["error"] as? String
            )
        }
        return ComputerSessionInfo(
            sessionId: dict["session_id"] as? String ?? "",
            targetUrl: dict["target_url"] as? String,
            targetDomain: dict["target_domain"] as? String,
            currentUrl: dict["current_url"] as? String,
            currentDomain: dict["current_domain"] as? String,
            status: ComputerSessionStatus(rawValue: dict["status"] as? String ?? "") ?? .created,
            browserStatus: ComputerBrowserStatus(rawValue: dict["browser_status"] as? String ?? "") ?? .notStarted,
            activeModel: dict["active_model"] as? String ?? "",
            turnIndex: dict["turn_index"] as? Int ?? 0,
            latestScreenshotPath: dict["latest_screenshot_path"] as? String,
            latestScreenshotTimestamp: (dict["latest_screenshot_ts"] as? Double).map { Date(timeIntervalSince1970: $0) },
            lastActionBatch: actions,
            lastActionResults: results,
            createdAt: Date(timeIntervalSince1970: dict["created_at"] as? Double ?? 0),
            updatedAt: Date(timeIntervalSince1970: dict["updated_at"] as? Double ?? 0),
            error: dict["error"] as? String,
            approvalPending: dict["approval_pending"] as? Bool ?? false,
            pendingApprovalId: dict["pending_approval_id"] as? String,
            domainAllowlisted: dict["domain_allowlisted"] as? Bool ?? false,
            task: dict["task"] as? String
        )
    }
}

struct ComputerActionInfo: Identifiable {
    let id = UUID()
    let type: String
    var x: Int? = nil
    var y: Int? = nil
    var text: String? = nil
    var key: String? = nil
    var url: String? = nil

    var summary: String {
        switch type {
        case "click", "double_click", "move":
            if let x, let y { return "\(type)(\(x), \(y))" }
            return type
        case "type":
            return "type \"\(text?.prefix(20) ?? "")…\""
        case "keypress":
            return "key(\(key ?? "?"))"
        case "scroll":
            return "scroll"
        case "navigate":
            return "→ \(url?.prefix(30) ?? "?")"
        case "wait":
            return "wait"
        default:
            return type
        }
    }
}

struct ComputerActionResultInfo: Identifiable {
    let id = UUID()
    let actionType: String
    let success: Bool
    var error: String? = nil
}

struct ComputerEventInfo: Identifiable {
    let id = UUID()
    let timestamp: Date
    let event: String
    let detail: String?

    /// Whether this event was triggered by the operator (user) vs the agent.
    var isOperatorEvent: Bool {
        switch event {
        case "approval_granted", "approval_denied", "approval_resumed", "paused", "cancelled":
            return true
        default:
            return false
        }
    }

    static func from(dict: [String: Any]) -> ComputerEventInfo? {
        guard let event = dict["event"] as? String else { return nil }
        let ts: Date
        if let t = dict["timestamp"] as? Double {
            ts = Date(timeIntervalSince1970: t)
        } else if let t = dict["ts"] as? Double {
            ts = Date(timeIntervalSince1970: t)
        } else {
            ts = Date()
        }
        let detail: String?
        if let d = dict["data"] as? [String: Any] {
            // Flatten data dict to a readable string
            let parts = d.compactMap { k, v -> String? in
                guard let s = v as? String ?? (v as? NSNumber)?.stringValue else { return nil }
                return "\(k): \(s)"
            }
            detail = parts.isEmpty ? nil : parts.joined(separator: ", ")
        } else {
            detail = dict["detail"] as? String
        }
        return ComputerEventInfo(timestamp: ts, event: event, detail: detail)
    }
}

struct ComputerCapabilitiesInfo {
    let playwrightInstalled: Bool
    let browsersInstalled: Bool
    let screenshotSupported: Bool
    let modelReady: Bool
    let model: String?
    let canRunSession: Bool
}
