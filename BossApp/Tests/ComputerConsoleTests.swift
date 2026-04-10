import XCTest
@testable import BossApp

// MARK: - Coordinate Mapper Tests

final class CoordinateMapperTests: XCTestCase {

    // MARK: - Exact Fit (no letterboxing)

    func testExactFitNoLetterboxing() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 1280, height: 800)
        )
        let rect = mapper.renderedRect
        XCTAssertEqual(rect.origin.x, 0, accuracy: 0.01)
        XCTAssertEqual(rect.origin.y, 0, accuracy: 0.01)
        XCTAssertEqual(rect.width, 1280, accuracy: 0.01)
        XCTAssertEqual(rect.height, 800, accuracy: 0.01)
    }

    func testExactFitPointMapping() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 1280, height: 800)
        )
        let pt = mapper.viewPoint(fromViewport: CGPoint(x: 640, y: 400))
        XCTAssertEqual(pt.x, 640, accuracy: 0.01)
        XCTAssertEqual(pt.y, 400, accuracy: 0.01)
    }

    // MARK: - Horizontal Letterboxing (container is taller)

    func testHorizontalLetterboxing() {
        // Container is 640×640 (square), viewport is 1280×800 (wide)
        // Scale = min(640/1280, 640/800) = min(0.5, 0.8) = 0.5
        // Rendered: 640×400, offset y = (640-400)/2 = 120
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 640, height: 640)
        )
        let rect = mapper.renderedRect
        XCTAssertEqual(rect.origin.x, 0, accuracy: 0.01)
        XCTAssertEqual(rect.origin.y, 120, accuracy: 0.01)
        XCTAssertEqual(rect.width, 640, accuracy: 0.01)
        XCTAssertEqual(rect.height, 400, accuracy: 0.01)
    }

    func testHorizontalLetterboxingPointMapping() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 640, height: 640)
        )
        // (0,0) in viewport → top-left of rendered rect
        let topLeft = mapper.viewPoint(fromViewport: CGPoint(x: 0, y: 0))
        XCTAssertEqual(topLeft.x, 0, accuracy: 0.01)
        XCTAssertEqual(topLeft.y, 120, accuracy: 0.01)

        // (1280,800) in viewport → bottom-right of rendered rect
        let bottomRight = mapper.viewPoint(fromViewport: CGPoint(x: 1280, y: 800))
        XCTAssertEqual(bottomRight.x, 640, accuracy: 0.01)
        XCTAssertEqual(bottomRight.y, 520, accuracy: 0.01)

        // Center point
        let center = mapper.viewPoint(fromViewport: CGPoint(x: 640, y: 400))
        XCTAssertEqual(center.x, 320, accuracy: 0.01)
        XCTAssertEqual(center.y, 320, accuracy: 0.01)
    }

    // MARK: - Vertical Letterboxing (container is wider)

    func testVerticalLetterboxing() {
        // Container is 800×400, viewport is 1280×800
        // Scale = min(800/1280, 400/800) = min(0.625, 0.5) = 0.5
        // Rendered: 640×400, offset x = (800-640)/2 = 80
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 800, height: 400)
        )
        let rect = mapper.renderedRect
        XCTAssertEqual(rect.origin.x, 80, accuracy: 0.01)
        XCTAssertEqual(rect.origin.y, 0, accuracy: 0.01)
        XCTAssertEqual(rect.width, 640, accuracy: 0.01)
        XCTAssertEqual(rect.height, 400, accuracy: 0.01)
    }

    func testVerticalLetterboxingPointMapping() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 800, height: 400)
        )
        // Click at viewport (100, 200) → view (80 + 100*0.5, 0 + 200*0.5) = (130, 100)
        let pt = mapper.viewPoint(fromViewport: CGPoint(x: 100, y: 200))
        XCTAssertEqual(pt.x, 130, accuracy: 0.01)
        XCTAssertEqual(pt.y, 100, accuracy: 0.01)
    }

    // MARK: - Uniform Scaling (half size)

    func testHalfScaleRendering() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 640, height: 400)
        )
        let rect = mapper.renderedRect
        XCTAssertEqual(rect.origin.x, 0, accuracy: 0.01)
        XCTAssertEqual(rect.origin.y, 0, accuracy: 0.01)
        XCTAssertEqual(rect.width, 640, accuracy: 0.01)
        XCTAssertEqual(rect.height, 400, accuracy: 0.01)

        let pt = mapper.viewPoint(fromViewport: CGPoint(x: 412, y: 308))
        XCTAssertEqual(pt.x, 206, accuracy: 0.01)
        XCTAssertEqual(pt.y, 154, accuracy: 0.01)
    }

    // MARK: - Zero / degenerate sizes

    func testZeroViewportReturnsZeroRect() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 0, height: 0),
            containerFrame: CGSize(width: 640, height: 400)
        )
        XCTAssertEqual(mapper.renderedRect, .zero)
    }

    func testZeroContainerReturnsZeroRect() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 0, height: 0)
        )
        XCTAssertEqual(mapper.renderedRect, .zero)
    }

    func testZeroSizesReturnZeroPoint() {
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 0, height: 0),
            containerFrame: CGSize(width: 0, height: 0)
        )
        let pt = mapper.viewPoint(fromViewport: CGPoint(x: 100, y: 200))
        XCTAssertEqual(pt, .zero)
    }

    // MARK: - Non-standard viewport

    func testNonStandardViewport() {
        // Viewport 1920×1080 into 960×540 → scale 0.5, no offset
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1920, height: 1080),
            containerFrame: CGSize(width: 960, height: 540)
        )
        let rect = mapper.renderedRect
        XCTAssertEqual(rect.origin.x, 0, accuracy: 0.01)
        XCTAssertEqual(rect.origin.y, 0, accuracy: 0.01)
        XCTAssertEqual(rect.width, 960, accuracy: 0.01)
        XCTAssertEqual(rect.height, 540, accuracy: 0.01)
    }

    // MARK: - Asymmetric letterboxing

    func testAsymmetricLetterboxing() {
        // Viewport 1280×800 into 400×200
        // Scale = min(400/1280, 200/800) = min(0.3125, 0.25) = 0.25
        // Rendered: 320×200, offset x = (400-320)/2 = 40
        let mapper = CoordinateMapper(
            viewport: CGSize(width: 1280, height: 800),
            containerFrame: CGSize(width: 400, height: 200)
        )
        let rect = mapper.renderedRect
        XCTAssertEqual(rect.origin.x, 40, accuracy: 0.01)
        XCTAssertEqual(rect.origin.y, 0, accuracy: 0.01)
        XCTAssertEqual(rect.width, 320, accuracy: 0.01)
        XCTAssertEqual(rect.height, 200, accuracy: 0.01)

        // Verify a point in the rendered area
        let pt = mapper.viewPoint(fromViewport: CGPoint(x: 640, y: 400))
        // x = 40 + 640*0.25 = 200, y = 0 + 400*0.25 = 100
        XCTAssertEqual(pt.x, 200, accuracy: 0.01)
        XCTAssertEqual(pt.y, 100, accuracy: 0.01)
    }
}

// MARK: - Session Status Transition Tests

final class SessionStatusTransitionTests: XCTestCase {

    func testTerminalStatuses() {
        XCTAssertTrue(ComputerSessionStatus.completed.isTerminal)
        XCTAssertTrue(ComputerSessionStatus.failed.isTerminal)
        XCTAssertTrue(ComputerSessionStatus.cancelled.isTerminal)
    }

    func testNonTerminalStatuses() {
        XCTAssertFalse(ComputerSessionStatus.created.isTerminal)
        XCTAssertFalse(ComputerSessionStatus.launching.isTerminal)
        XCTAssertFalse(ComputerSessionStatus.running.isTerminal)
        XCTAssertFalse(ComputerSessionStatus.paused.isTerminal)
        XCTAssertFalse(ComputerSessionStatus.waitingApproval.isTerminal)
    }

    func testStatusRawValues() {
        // Ensure raw values match backend snake_case
        XCTAssertEqual(ComputerSessionStatus.waitingApproval.rawValue, "waiting_approval")
        XCTAssertEqual(ComputerSessionStatus.running.rawValue, "running")
        XCTAssertEqual(ComputerSessionStatus.paused.rawValue, "paused")
        XCTAssertEqual(ComputerSessionStatus.created.rawValue, "created")
    }

    func testStatusLabels() {
        XCTAssertEqual(ComputerSessionStatus.running.label, "Running")
        XCTAssertEqual(ComputerSessionStatus.waitingApproval.label, "Waiting Approval")
        XCTAssertEqual(ComputerSessionStatus.paused.label, "Paused")
    }
}

// MARK: - Session Info Parsing Tests

final class SessionInfoParsingTests: XCTestCase {

    func testFromDictBasic() {
        let dict: [String: Any] = [
            "session_id": "abc123",
            "target_url": "https://example.com",
            "target_domain": "example.com",
            "status": "running",
            "browser_status": "active",
            "active_model": "gpt-5.4",
            "turn_index": 5,
            "created_at": 1712700000.0,
            "updated_at": 1712700010.0,
            "approval_pending": false,
            "domain_allowlisted": true,
            "last_action_batch": [],
            "last_action_results": [],
        ]
        let info = ComputerSessionInfo.from(dict: dict)
        XCTAssertEqual(info.sessionId, "abc123")
        XCTAssertEqual(info.status, .running)
        XCTAssertEqual(info.targetDomain, "example.com")
        XCTAssertEqual(info.turnIndex, 5)
        XCTAssertFalse(info.approvalPending)
        XCTAssertTrue(info.domainAllowlisted)
        XCTAssertNil(info.pendingApprovalId)
    }

    func testFromDictWithApproval() {
        let dict: [String: Any] = [
            "session_id": "def456",
            "status": "waiting_approval",
            "browser_status": "active",
            "active_model": "gpt-5.4",
            "turn_index": 3,
            "created_at": 1712700000.0,
            "updated_at": 1712700010.0,
            "approval_pending": true,
            "pending_approval_id": "approval-789",
            "last_action_batch": [
                ["type": "type", "text": "password123"],
            ],
            "last_action_results": [],
        ]
        let info = ComputerSessionInfo.from(dict: dict)
        XCTAssertEqual(info.status, .waitingApproval)
        XCTAssertTrue(info.approvalPending)
        XCTAssertEqual(info.pendingApprovalId, "approval-789")
        XCTAssertEqual(info.lastActionBatch.count, 1)
        XCTAssertEqual(info.lastActionBatch.first?.type, "type")
        XCTAssertEqual(info.lastActionBatch.first?.text, "password123")
    }

    func testFromDictWithActionCoordinates() {
        let dict: [String: Any] = [
            "session_id": "xyz",
            "status": "running",
            "browser_status": "active",
            "active_model": "gpt-5.4",
            "turn_index": 1,
            "created_at": 1712700000.0,
            "updated_at": 1712700010.0,
            "approval_pending": false,
            "last_action_batch": [
                ["type": "click", "x": 412, "y": 308],
                ["type": "double_click", "x": 100, "y": 200],
            ],
            "last_action_results": [
                ["action_type": "click", "success": true],
                ["action_type": "double_click", "success": false, "error": "element not found"],
            ],
        ]
        let info = ComputerSessionInfo.from(dict: dict)
        XCTAssertEqual(info.lastActionBatch.count, 2)
        XCTAssertEqual(info.lastActionBatch[0].x, 412)
        XCTAssertEqual(info.lastActionBatch[0].y, 308)
        XCTAssertEqual(info.lastActionBatch[1].type, "double_click")
        XCTAssertEqual(info.lastActionResults.count, 2)
        XCTAssertTrue(info.lastActionResults[0].success)
        XCTAssertFalse(info.lastActionResults[1].success)
        XCTAssertEqual(info.lastActionResults[1].error, "element not found")
    }

    func testFromDictMissingFieldsUseDefaults() {
        let dict: [String: Any] = [:]
        let info = ComputerSessionInfo.from(dict: dict)
        XCTAssertEqual(info.sessionId, "")
        XCTAssertEqual(info.status, .created)
        XCTAssertEqual(info.turnIndex, 0)
        XCTAssertFalse(info.approvalPending)
        XCTAssertFalse(info.domainAllowlisted)
        XCTAssertNil(info.task)
    }

    func testFromDictWithCurrentDomain() {
        let dict: [String: Any] = [
            "session_id": "nav1",
            "target_url": "https://example.com",
            "target_domain": "example.com",
            "current_url": "https://other.com/page",
            "current_domain": "other.com",
            "status": "running",
            "browser_status": "active",
            "active_model": "gpt-5.4",
            "turn_index": 3,
            "created_at": 1712700000.0,
            "updated_at": 1712700010.0,
            "approval_pending": false,
            "domain_allowlisted": false,
            "last_action_batch": [],
            "last_action_results": [],
        ]
        let info = ComputerSessionInfo.from(dict: dict)
        XCTAssertEqual(info.targetDomain, "example.com")
        XCTAssertEqual(info.currentUrl, "https://other.com/page")
        XCTAssertEqual(info.currentDomain, "other.com")
    }

    func testFromDictCurrentDomainDefaultsNil() {
        let dict: [String: Any] = [
            "session_id": "nav2",
            "target_domain": "example.com",
            "status": "running",
            "browser_status": "active",
            "active_model": "gpt-5.4",
            "turn_index": 1,
            "created_at": 1712700000.0,
            "updated_at": 1712700010.0,
            "approval_pending": false,
            "last_action_batch": [],
            "last_action_results": [],
        ]
        let info = ComputerSessionInfo.from(dict: dict)
        XCTAssertNil(info.currentUrl)
        XCTAssertNil(info.currentDomain)
    }
}

// MARK: - Event Info Parsing Tests

final class EventInfoParsingTests: XCTestCase {

    func testFromDictBasic() {
        let dict: [String: Any] = [
            "event": "action_executed",
            "timestamp": 1712700000.0,
            "data": ["action": "click", "x": "412", "y": "308"],
        ]
        let event = ComputerEventInfo.from(dict: dict)
        XCTAssertNotNil(event)
        XCTAssertEqual(event?.event, "action_executed")
        XCTAssertFalse(event?.isOperatorEvent ?? true)
    }

    func testOperatorEvent() {
        for eventName in ["approval_granted", "approval_denied", "approval_resumed", "paused", "cancelled"] {
            let dict: [String: Any] = ["event": eventName, "timestamp": 1712700000.0]
            let event = ComputerEventInfo.from(dict: dict)
            XCTAssertTrue(event?.isOperatorEvent ?? false, "\(eventName) should be operator event")
        }
    }

    func testAgentEvent() {
        for eventName in ["action_executed", "screenshot", "turn_completed", "navigated"] {
            let dict: [String: Any] = ["event": eventName, "timestamp": 1712700000.0]
            let event = ComputerEventInfo.from(dict: dict)
            XCTAssertFalse(event?.isOperatorEvent ?? true, "\(eventName) should be agent event")
        }
    }

    func testFromDictMissingEvent() {
        let dict: [String: Any] = ["timestamp": 1712700000.0]
        let event = ComputerEventInfo.from(dict: dict)
        XCTAssertNil(event)
    }
}

// MARK: - Action Summary Tests

final class ActionSummaryTests: XCTestCase {

    func testClickSummary() {
        let action = ComputerActionInfo(type: "click", x: 100, y: 200)
        XCTAssertEqual(action.summary, "click(100, 200)")
    }

    func testTypeSummary() {
        let action = ComputerActionInfo(type: "type", text: "hello world")
        XCTAssertTrue(action.summary.contains("type"))
        XCTAssertTrue(action.summary.contains("hello world"))
    }

    func testKeypressSummary() {
        let action = ComputerActionInfo(type: "keypress", key: "Enter")
        XCTAssertEqual(action.summary, "key(Enter)")
    }

    func testNavigateSummary() {
        let action = ComputerActionInfo(type: "navigate", url: "https://example.com/page")
        XCTAssertTrue(action.summary.contains("→"))
        XCTAssertTrue(action.summary.contains("example.com"))
    }

    func testScrollSummary() {
        let action = ComputerActionInfo(type: "scroll")
        XCTAssertEqual(action.summary, "scroll")
    }
}
