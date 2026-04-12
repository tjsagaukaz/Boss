import Foundation

enum BackendBootstrapResult: Sendable {
    case ready
    case started
    case warning(String)
    case failure(String)
}

actor LocalBackendBootstrapper {
    static let shared = LocalBackendBootstrapper()

    private let startupDelayNanoseconds: UInt64 = 250_000_000
    private let startupAttempts = 24
    private let launchLogTailBytes: UInt64 = 16 * 1024

    func ensureBackendReady(api: APIClient) async -> BackendBootstrapResult {
        guard let workspaceRoot = resolveWorkspaceRoot() else {
            return .failure("Couldn't locate the local Boss workspace. Launch the backend once with ./start-server.sh.")
        }

        let interpreter = workspaceRoot.appendingPathComponent(".venv/bin/python")
        do {
            let status = try await api.fetchSystemStatus()
            if statusMatches(status, expectedWorkspaceRoot: workspaceRoot, expectedInterpreter: interpreter) {
                return .ready
            }
            return .warning(mismatchMessage(for: status, expectedWorkspaceRoot: workspaceRoot, expectedInterpreter: interpreter))
        } catch {
            // Status fetch failed — backend may not be running yet.
        }

        guard FileManager.default.isExecutableFile(atPath: interpreter.path) else {
            return .failure("Missing local interpreter at \(interpreter.path).")
        }

        do {
            let launched = try launchBackend(workspaceRoot: workspaceRoot, interpreter: interpreter)
            return await waitForBackend(api: api, workspaceRoot: workspaceRoot, interpreter: interpreter, launched: launched)
        } catch {
            return .failure("Couldn't start the local backend. \(error.localizedDescription)")
        }
    }

    private func waitForBackend(
        api: APIClient,
        workspaceRoot: URL,
        interpreter: URL,
        launched: LaunchedBackend
    ) async -> BackendBootstrapResult {
        for _ in 0..<startupAttempts {
            if let status = try? await api.fetchSystemStatus(),
               statusMatches(status, expectedWorkspaceRoot: workspaceRoot, expectedInterpreter: interpreter) {
                return .started
            }

            if !launched.process.isRunning {
                let output = launchOutput(from: launched).nonEmpty ?? "The local backend exited before becoming ready."
                return .failure(output)
            }

            try? await Task.sleep(nanoseconds: startupDelayNanoseconds)
        }

        let output = launchOutput(from: launched).nonEmpty ?? "Timed out waiting for the local backend on 127.0.0.1:8321."
        return .failure(output)
    }

    private func launchBackend(workspaceRoot: URL, interpreter: URL) throws -> LaunchedBackend {
        let process = Process()
        let logFileURL = backendLogFileURL()
        try ensureLogFileExists(at: logFileURL)

        let logHandle = try FileHandle(forWritingTo: logFileURL)
        defer { try? logHandle.close() }
        let logOffset = try logHandle.seekToEnd()

        process.executableURL = interpreter
        process.arguments = [
            "-m", "uvicorn", "boss.api:app",
            "--host", "127.0.0.1",
            "--port", "8321",
        ]
        process.currentDirectoryURL = workspaceRoot

        var environment = ProcessInfo.processInfo.environment
        environment["BOSS_API_PORT"] = "8321"
        environment["BOSS_APP_AUTOSTART"] = "1"
        process.environment = environment
        process.standardOutput = logHandle
        process.standardError = logHandle

        try process.run()
        return LaunchedBackend(process: process, logFileURL: logFileURL, logOffset: logOffset)
    }

    private func statusMatches(
        _ status: SystemStatusInfo,
        expectedWorkspaceRoot: URL,
        expectedInterpreter: URL
    ) -> Bool {
        guard let workspacePath = status.workspacePath,
              let interpreterPath = status.interpreterPath,
              status.processId != nil else {
            return false
        }

        if !pathsMatch(workspacePath, expectedWorkspaceRoot.path) {
            return false
        }
        if !pathsMatch(interpreterPath, expectedInterpreter.path) {
            return false
        }
        if let warnings = status.runtimeTrust?.warnings, !warnings.isEmpty {
            return false
        }
        return true
    }

    private func mismatchMessage(
        for status: SystemStatusInfo,
        expectedWorkspaceRoot: URL,
        expectedInterpreter: URL
    ) -> String {
        let workspace = status.workspacePath ?? "unknown workspace"
        let interpreter = status.interpreterPath ?? "unknown interpreter"
        let warnings = status.runtimeTrust?.warnings ?? []
        let warningSuffix = warnings.isEmpty ? "" : " Warnings: \(warnings.joined(separator: ", "))."
        return "Boss is connected to a different backend on 127.0.0.1:8321 (workspace \(workspace), interpreter \(interpreter)). Expected workspace \(expectedWorkspaceRoot.path) and interpreter \(expectedInterpreter.path). Stop the old server and relaunch the app.\(warningSuffix)"
    }

    private func resolveWorkspaceRoot() -> URL? {
        var candidates: [URL] = []
        let env = ProcessInfo.processInfo.environment
        if let configuredRoot = env["BOSS_WORKSPACE_ROOT"], !configuredRoot.isEmpty {
            candidates.append(URL(fileURLWithPath: configuredRoot))
        }

        candidates.append(URL(fileURLWithPath: FileManager.default.currentDirectoryPath))

        if let executableURL = Bundle.main.executableURL {
            candidates.append(contentsOf: ancestorChain(startingAt: executableURL.deletingLastPathComponent()))
        }

        candidates.append(contentsOf: ancestorChain(startingAt: Bundle.main.bundleURL))

        // Generic home-relative fallback
        let homeBoss = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("boss")
        candidates.append(homeBoss)

        var seen: Set<String> = []
        for candidate in candidates {
            let normalized = candidate.standardizedFileURL
            guard seen.insert(normalized.path).inserted else { continue }
            if isWorkspaceRoot(normalized) {
                return normalized
            }
        }
        return nil
    }

    private func ancestorChain(startingAt url: URL) -> [URL] {
        var chain: [URL] = []
        var current = url.standardizedFileURL

        while true {
            chain.append(current)
            let parent = current.deletingLastPathComponent()
            if parent.path == current.path {
                break
            }
            current = parent
        }

        return chain
    }

    private func isWorkspaceRoot(_ url: URL) -> Bool {
        let fileManager = FileManager.default
        let interpreter = url.appendingPathComponent(".venv/bin/python")
        let startScript = url.appendingPathComponent("start-server.sh")
        let apiModule = url.appendingPathComponent("boss/api.py")
        return fileManager.isExecutableFile(atPath: interpreter.path)
            && fileManager.fileExists(atPath: startScript.path)
            && fileManager.fileExists(atPath: apiModule.path)
    }

    private func pathsMatch(_ lhs: String, _ rhs: String) -> Bool {
        URL(fileURLWithPath: lhs).standardizedFileURL.path == URL(fileURLWithPath: rhs).standardizedFileURL.path
    }

    private func launchOutput(from launched: LaunchedBackend) -> String {
        guard let handle = try? FileHandle(forReadingFrom: launched.logFileURL) else {
            return ""
        }
        defer {
            try? handle.close()
        }

        do {
            let endOffset = try handle.seekToEnd()
            let readStart = max(launched.logOffset, endOffset > launchLogTailBytes ? endOffset - launchLogTailBytes : 0)
            try handle.seek(toOffset: readStart)
            let data = try handle.readToEnd() ?? Data()
            return String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        } catch {
            return ""
        }
    }

    private func backendLogFileURL() -> URL {
        let logsDirectory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".boss", isDirectory: true)
            .appendingPathComponent("logs", isDirectory: true)
        return logsDirectory.appendingPathComponent("autostart-backend.log")
    }

    private func ensureLogFileExists(at url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: Data())
        }
    }
}

private struct LaunchedBackend {
    let process: Process
    let logFileURL: URL
    let logOffset: UInt64
}

private extension String {
    var nonEmpty: String? {
        isEmpty ? nil : self
    }
}