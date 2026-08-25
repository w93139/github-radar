import AppKit
import Foundation
import WebKit

private struct ReadyPayload: Decodable {
    let service: String
    let port: Int
    let pid: Int32
}

final class GitHubRadarApp: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let maxRestartAttempts = 3
    private var window: NSWindow?
    private var webView: WKWebView?
    private var serverProcess: Process?
    private var serverPort: Int?
    private var readyFileURL: URL?
    private var logHandle: FileHandle?
    private var healthTimer: Timer?
    private var healthAttempts = 0
    private var consecutiveHealthFailures = 0
    private var restartAttempts = 0
    private var isStartingServer = false
    private var isTerminating = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        configureMainMenu()
        startServer()
    }

    private func configureMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "关于 GitHub Radar", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "退出 GitHub Radar", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appItem.submenu = appMenu

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "显示")
        let reload = NSMenuItem(title: "刷新界面", action: #selector(reloadDashboard), keyEquivalent: "r")
        reload.target = self
        viewMenu.addItem(reload)
        viewItem.submenu = viewMenu
        NSApp.mainMenu = mainMenu
    }

    private func startServer() {
        guard !isTerminating, !isStartingServer else { return }
        if let process = serverProcess, process.isRunning { return }
        guard let resourceRoot = Bundle.main.resourceURL?.appendingPathComponent("GitHubRadar"),
              FileManager.default.fileExists(atPath: resourceRoot.appendingPathComponent("scripts/github_radar_ui.py").path)
        else {
            showFatalError("应用资源不完整，请重新安装 GitHub Radar。")
            return
        }

        isStartingServer = true
        healthAttempts = 0
        consecutiveHealthFailures = 0
        serverPort = nil
        removeReadyFile()
        readyFileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("github-radar-\(ProcessInfo.processInfo.processIdentifier)-\(UUID().uuidString).json")

        prepareLogHandle()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [
            resourceRoot.appendingPathComponent("scripts/github_radar_ui.py").path,
            "--no-open", "--port", "0",
            "--ready-file", readyFileURL!.path,
        ]
        process.currentDirectoryURL = resourceRoot
        var environment = ProcessInfo.processInfo.environment
        let localBin = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin").path
        environment["PATH"] = "\(localBin):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardOutput = logHandle
        process.standardError = logHandle
        process.terminationHandler = { [weak self] task in
            DispatchQueue.main.async { self?.serverDidTerminate(task) }
        }

        do {
            try process.run()
            serverProcess = process
            pollUntilReady(process)
        } catch {
            isStartingServer = false
            scheduleRestart(reason: "无法启动本地服务：\(error.localizedDescription)")
        }
    }

    private func prepareLogHandle() {
        try? logHandle?.close()
        let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logURL = logs.appendingPathComponent("GitHub Radar.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        logHandle = try? FileHandle(forWritingTo: logURL)
        _ = try? logHandle?.seekToEnd()
    }

    private func pollUntilReady(_ process: Process) {
        guard process === serverProcess, process.isRunning, !isTerminating else { return }
        if let readyFileURL,
           let data = try? Data(contentsOf: readyFileURL),
           let payload = try? JSONDecoder().decode(ReadyPayload.self, from: data),
           payload.service == "github-radar", payload.pid == process.processIdentifier,
           (1024...65535).contains(payload.port) {
            serverPort = payload.port
            checkHealth(port: payload.port) { [weak self] running in
                guard let self, process === self.serverProcess else { return }
                if running {
                    self.serverBecameReady(process)
                } else {
                    self.continueReadinessPolling(process)
                }
            }
            return
        }
        continueReadinessPolling(process)
    }

    private func continueReadinessPolling(_ process: Process) {
        healthAttempts += 1
        if healthAttempts < 50 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                self?.pollUntilReady(process)
            }
        } else {
            process.terminationHandler = nil
            if process.isRunning { process.terminate() }
            serverProcess = nil
            isStartingServer = false
            scheduleRestart(reason: "本地服务启动超时。")
        }
    }

    private func serverBecameReady(_ process: Process) {
        isStartingServer = false
        healthAttempts = 0
        consecutiveHealthFailures = 0
        showWindow()
        beginHealthMonitoring()
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self, weak process] in
            guard let self, let process, process === self.serverProcess, process.isRunning else { return }
            self.restartAttempts = 0
        }
    }

    private func beginHealthMonitoring() {
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 8, repeats: true) { [weak self] _ in
            guard let self, !self.isTerminating else { return }
            self.checkHealth { running in
                if running {
                    self.consecutiveHealthFailures = 0
                } else {
                    self.consecutiveHealthFailures += 1
                    if self.consecutiveHealthFailures >= 3 {
                        self.scheduleRestart(reason: "本地服务失去响应。")
                    }
                }
            }
        }
    }

    private func serverDidTerminate(_ task: Process) {
        guard task === serverProcess else { return }
        serverProcess = nil
        serverPort = nil
        isStartingServer = false
        healthTimer?.invalidate()
        healthTimer = nil
        removeReadyFile()
        guard !isTerminating else { return }
        scheduleRestart(reason: "本地服务意外停止（状态 \(task.terminationStatus)）。")
    }

    private func scheduleRestart(reason: String) {
        guard !isTerminating else { return }
        healthTimer?.invalidate()
        healthTimer = nil
        if let process = serverProcess {
            process.terminationHandler = nil
            if process.isRunning { process.terminate() }
        }
        serverProcess = nil
        serverPort = nil
        isStartingServer = false
        removeReadyFile()

        guard restartAttempts < maxRestartAttempts else {
            showFatalError("本地数据服务连续恢复失败。\n\n\(reason)\n\n诊断日志：~/Library/Logs/GitHub Radar.log")
            return
        }
        restartAttempts += 1
        let delay = min(2.0, 0.4 * Double(restartAttempts))
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in self?.startServer() }
    }

    private func checkHealth(port: Int? = nil, completion: @escaping (Bool) -> Void) {
        guard let activePort = port ?? serverPort,
              let url = URL(string: "http://127.0.0.1:\(activePort)/api/health") else {
            completion(false)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1
        URLSession.shared.dataTask(with: request) { data, response, _ in
            let ok = (response as? HTTPURLResponse)?.statusCode == 200
                && data.flatMap { String(data: $0, encoding: .utf8) }?.contains("github-radar") == true
            DispatchQueue.main.async { completion(ok) }
        }.resume()
    }

    private func showWindow() {
        if window == nil {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .default()
            let webView = WKWebView(frame: .zero, configuration: configuration)
            webView.setValue(false, forKey: "drawsBackground")

            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                backing: .buffered,
                defer: false
            )
            window.title = "GitHub Radar"
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.backgroundColor = .black
            window.minSize = NSSize(width: 860, height: 600)
            window.contentView = webView
            window.delegate = self
            window.center()
            self.window = window
            self.webView = webView
        }
        loadDashboard()
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func loadDashboard() {
        guard let serverPort, let url = URL(string: "http://127.0.0.1:\(serverPort)") else { return }
        if webView?.url?.port != serverPort {
            webView?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10))
        } else {
            webView?.reload()
        }
    }

    @objc private func reloadDashboard() {
        checkHealth { [weak self] running in
            guard let self else { return }
            if running { self.loadDashboard() } else { self.scheduleRestart(reason: "手动刷新时服务不可用。") }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if let process = serverProcess, process.isRunning, serverPort != nil {
            showWindow()
        } else {
            startServer()
        }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    private func showFatalError(_ message: String) {
        guard !isTerminating else { return }
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "GitHub Radar 无法恢复"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }

    private func removeReadyFile() {
        if let readyFileURL { try? FileManager.default.removeItem(at: readyFileURL) }
        readyFileURL = nil
    }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        healthTimer?.invalidate()
        healthTimer = nil
        if let process = serverProcess {
            process.terminationHandler = nil
            if process.isRunning {
                process.terminate()
                process.waitUntilExit()
            }
        }
        serverProcess = nil
        removeReadyFile()
        try? logHandle?.close()
    }
}
