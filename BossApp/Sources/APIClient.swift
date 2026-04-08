import Foundation

// MARK: - SSE Event Parser

struct SSEEvent: Sendable {
    var type: String = ""
    var data: [String: String] = [:]
}

enum APIError: LocalizedError {
    case invalidURL(String)
    case invalidResponse
    case transport(String)
    case http(statusCode: Int, message: String)
    case decoding(context: String, message: String)

    var userMessage: String {
        switch self {
        case .invalidURL(let path):
            return "Invalid request URL for \(path)."
        case .invalidResponse:
            return "The server returned an invalid response."
        case .transport(let message):
            return "Couldn't reach Boss. \(message)"
        case .http(let statusCode, let message):
            return "Request failed (\(statusCode)): \(message)"
        case .decoding(let context, let message):
            return "Unexpected response from \(context). \(message)"
        }
    }

    var errorDescription: String? {
        userMessage
    }
}

// MARK: - API Client

final class APIClient: Sendable {
    static let shared = APIClient()

    let baseURL: String
    private let session: URLSession

    init(baseURL: String = "http://127.0.0.1:8321") {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 120
        config.httpMaximumConnectionsPerHost = 4
        self.session = URLSession(configuration: config)
    }

    // MARK: - Streaming Chat

    func streamChat(message: String, sessionId: String?) -> AsyncStream<SSEEvent> {
        var req = URLRequest(url: URL(string: "\(baseURL)/api/chat")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")

        struct Body: Encodable {
            let message: String
            let session_id: String?
        }

        req.httpBody = try? JSONEncoder().encode(Body(message: message, session_id: sessionId))
        return stream(request: req)
    }

    func streamPermissionDecision(
        runId: String,
        approvalId: String,
        decision: PermissionDecision
    ) -> AsyncStream<SSEEvent> {
        var req = URLRequest(url: URL(string: "\(baseURL)/api/chat/permissions")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")

        struct Body: Encodable {
            let run_id: String
            let approval_id: String
            let decision: String
        }

        req.httpBody = try? JSONEncoder().encode(
            Body(run_id: runId, approval_id: approvalId, decision: decision.rawValue)
        )
        return stream(request: req)
    }

    // MARK: - REST Endpoints

    func fetchProjects() async throws -> [ProjectInfo] {
        let data = try await get("/api/memory/projects")
        return try decode([ProjectInfo].self, from: data, context: "/api/memory/projects")
    }

    func fetchFacts() async throws -> [FactInfo] {
        let data = try await get("/api/memory/facts")
        return try decode([FactInfo].self, from: data, context: "/api/memory/facts")
    }

    func fetchStats() async throws -> MemoryStats {
        let data = try await get("/api/memory/stats")
        return try decode(
            MemoryStats.self,
            from: data,
            context: "/api/memory/stats",
            dateDecodingStrategy: .iso8601
        )
    }

    func fetchMemoryOverview(sessionId: String?, message: String?) async throws -> MemoryOverview {
        var items: [URLQueryItem] = []
        if let sessionId, !sessionId.isEmpty {
            items.append(URLQueryItem(name: "session_id", value: sessionId))
        }
        if let message, !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            items.append(URLQueryItem(name: "message", value: message))
        }
        let data = try await get("/api/memory/overview", queryItems: items)
        return try decode(
            MemoryOverview.self,
            from: data,
            context: "/api/memory/overview",
            dateDecodingStrategy: .iso8601
        )
    }

    func fetchPermissions() async throws -> [PermissionEntry] {
        let data = try await get("/api/permissions")
        return try decode(
            [PermissionEntry].self,
            from: data,
            context: "/api/permissions",
            dateDecodingStrategy: .iso8601
        )
    }

    func fetchSystemStatus() async throws -> SystemStatusInfo {
        let data = try await get("/api/system/status")
        return try decode(
            SystemStatusInfo.self,
            from: data,
            context: "/api/system/status",
            dateDecodingStrategy: .iso8601
        )
    }

    func revokePermission(tool: String, scopeKey: String) async throws {
        _ = try await delete("/api/permissions", queryItems: [
            URLQueryItem(name: "tool", value: tool),
            URLQueryItem(name: "scope_key", value: scopeKey),
        ])
    }

    func deleteMemoryItem(sourceTable: String, itemId: Int) async throws {
        _ = try await delete("/api/memory/items/\(sourceTable)/\(itemId)")
    }

    func triggerScan() async throws -> [String: Any] {
        let data = try await post("/api/system/scan", body: nil)
        return try decodeJSONObject(from: data, context: "/api/system/scan")
    }

    // MARK: - HTTP helpers

    private func get(_ path: String, queryItems: [URLQueryItem] = []) async throws -> Data {
        try await request(method: "GET", path: path, queryItems: queryItems)
    }

    private func post(_ path: String, body: Data?) async throws -> Data {
        try await request(method: "POST", path: path, body: body)
    }

    private func delete(_ path: String, queryItems: [URLQueryItem] = []) async throws -> Data {
        try await request(method: "DELETE", path: path, queryItems: queryItems)
    }

    private func request(
        method: String,
        path: String,
        queryItems: [URLQueryItem] = [],
        body: Data? = nil
    ) async throws -> Data {
        let url = try buildURL(path: path, queryItems: queryItems)
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.httpBody = body
        if body != nil {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return try await send(req)
    }

    private func buildURL(path: String, queryItems: [URLQueryItem] = []) throws -> URL {
        guard var components = URLComponents(string: "\(baseURL)\(path)") else {
            throw APIError.invalidURL(path)
        }
        if !queryItems.isEmpty {
            components.queryItems = queryItems
        }
        guard let url = components.url else {
            throw APIError.invalidURL(path)
        }
        return url
    }

    private func send(_ request: URLRequest) async throws -> Data {
        do {
            let (data, response) = try await session.data(for: request)
            try validate(response: response, data: data)
            return data
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let message = serverMessage(from: data)
                ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode).capitalized
            throw APIError.http(statusCode: http.statusCode, message: message)
        }
    }

    private func decode<T: Decodable>(
        _ type: T.Type,
        from data: Data,
        context: String,
        dateDecodingStrategy: JSONDecoder.DateDecodingStrategy = .deferredToDate
    ) throws -> T {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = dateDecodingStrategy
        do {
            return try decoder.decode(type, from: data)
        } catch {
            throw APIError.decoding(context: context, message: Self.describeDecodingError(error))
        }
    }

    private func decodeJSONObject(from data: Data, context: String) throws -> [String: Any] {
        do {
            let object = try JSONSerialization.jsonObject(with: data)
            guard let dictionary = object as? [String: Any] else {
                throw APIError.decoding(context: context, message: "Expected a JSON object.")
            }
            return dictionary
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError.decoding(context: context, message: error.localizedDescription)
        }
    }

    private func serverMessage(from data: Data) -> String? {
        guard !data.isEmpty else { return nil }

        if let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for key in ["detail", "message", "error"] {
                if let value = payload[key] as? String, !value.isEmpty {
                    return value
                }
            }
        }

        let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let text, !text.isEmpty {
            return text
        }
        return nil
    }

    private static func describeDecodingError(_ error: Error) -> String {
        switch error {
        case let DecodingError.keyNotFound(key, context):
            return "Missing key '\(key.stringValue)' at \(codingPathDescription(context.codingPath))."
        case let DecodingError.typeMismatch(_, context):
            return "Type mismatch at \(codingPathDescription(context.codingPath)): \(context.debugDescription)"
        case let DecodingError.valueNotFound(_, context):
            return "Missing value at \(codingPathDescription(context.codingPath)): \(context.debugDescription)"
        case let DecodingError.dataCorrupted(context):
            return context.debugDescription
        default:
            return error.localizedDescription
        }
    }

    private static func codingPathDescription(_ codingPath: [CodingKey]) -> String {
        guard !codingPath.isEmpty else {
            return "the top level"
        }
        return codingPath.map(\.stringValue).joined(separator: ".")
    }

    private func stream(request: URLRequest) -> AsyncStream<SSEEvent> {
        let sess = session
        return AsyncStream { continuation in
            let task = Task {
                do {
                    let (stream, response) = try await sess.bytes(for: request)

                    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                        var evt = SSEEvent()
                        evt.type = "error"
                        evt.data = ["message": "Server returned an error"]
                        continuation.yield(evt)
                        continuation.finish()
                        return
                    }

                    var rawBuffer = Data()
                    for try await byte in stream {
                        rawBuffer.append(byte)

                        guard rawBuffer.count >= 2,
                              rawBuffer.suffix(2) == Data([0x0A, 0x0A]) else { continue }

                        guard let bufferStr = String(data: rawBuffer, encoding: .utf8) else {
                            continue
                        }

                        let lines = bufferStr.split(separator: "\n", omittingEmptySubsequences: false)
                        var event = SSEEvent()

                        for line in lines {
                            let lineStr = String(line)
                            if lineStr.hasPrefix("data: ") {
                                let jsonStr = String(lineStr.dropFirst(6))
                                if let jsonData = jsonStr.data(using: .utf8),
                                   let dict = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
                                    var stringDict: [String: String] = [:]
                                    for (key, value) in dict {
                                        stringDict[key] = "\(value)"
                                    }
                                    event.data = stringDict
                                    event.type = stringDict["type"] ?? ""
                                }
                            } else if lineStr.hasPrefix("event: ") {
                                event.type = String(lineStr.dropFirst(7))
                            }
                        }

                        if !event.type.isEmpty {
                            let isDone = event.type == "done"
                            continuation.yield(event)
                            if isDone { break }
                        }
                        rawBuffer.removeAll(keepingCapacity: true)
                    }
                } catch {
                    var evt = SSEEvent()
                    evt.type = "error"
                    evt.data = ["message": "Connection failed: \(error.localizedDescription)"]
                    continuation.yield(evt)
                }
                continuation.finish()
            }

            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
