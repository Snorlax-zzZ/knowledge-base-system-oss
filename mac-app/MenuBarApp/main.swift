import AppKit
import Foundation

enum ServiceState {
    case running
    case stopped
    case busy
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSUserNotificationCenterDelegate {
    private var statusItem: NSStatusItem!
    private var refreshTimer: Timer?
    private var serviceState: ServiceState = .stopped
    private var projectRoot: String = "/Applications/KnowledgeBase"

    private var startItem: NSMenuItem!
    private var stopItem: NSMenuItem!

    // Embedding 服务壳层 manager(kb-api 健康后才拉起,只拉一次)
    private var embeddingManager: EmbeddingProcessManager?
    private var embeddingStarted = false

    // Embedding 手动启动菜单项(对齐 Win 侧 tray_app_local.py "▶ 启动 Embedding 服务"):
    // installed=true + running=false 时可点,给 auto-bootstrap 失败 / 用户手动 stop 后
    // 的场景一个不用重启壳层就能重拉 infinity 的兜底通道。
    private var embeddingStartItem: NSMenuItem!
    // 点击后到 completion 期间置灰防连点。busy 期间菜单标签置成"启动 Embedding 服务（进行中）"。
    private var embeddingManualStartInFlight = false

    // rebuild 状态轮询的单实例保护：托盘菜单触发重建后开后台 poll，跑到 completed/failed 弹通知；
    // 用户连点不开多个 poller 并发刷屏通知。
    private var rebuildPollerActive = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        projectRoot = resolveProjectRoot()
        setupStatusBar()
        refreshStatus()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 4.0, repeats: true) { [weak self] _ in
            self?.refreshStatus()
        }
        // 让通知点击"显示"按钮时能弹出完整 message（banner 默认会截断长正文）
        NSUserNotificationCenter.default.delegate = self
    }

    // 隐藏承载 window：alert 挂 sheet 上不阻塞主 event loop，
    // 菜单栏"退出"仍能正常响应。alphaValue=0 用户不可见。
    private var alertHostWindow: NSWindow?

    // 用户点击通知（含"显示"按钮）→ 弹 NSAlert 展示完整 title + informativeText
    // 用 sheet-modal 而非 app-modal（runModal 会阻塞主线程 event loop，
    // 用户没法点菜单栏"退出"）。
    func userNotificationCenter(
        _ center: NSUserNotificationCenter,
        didActivate notification: NSUserNotification
    ) {
        let host: NSWindow
        if let existing = alertHostWindow {
            host = existing
        } else {
            host = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 1, height: 1),
                styleMask: [.titled],
                backing: .buffered,
                defer: false
            )
            host.isReleasedWhenClosed = false
            host.alphaValue = 0
            host.level = .floating
            alertHostWindow = host
        }
        host.center()
        host.orderFrontRegardless()

        let alert = NSAlert()
        alert.messageText = notification.title ?? ""
        alert.informativeText = notification.informativeText ?? ""
        alert.alertStyle = .informational
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.beginSheetModal(for: host) { [weak host] _ in
            host?.orderOut(nil)
        }
    }

    // App 在前台时也强制把通知 banner 弹出来（默认 macOS 会吞掉）
    func userNotificationCenter(
        _ center: NSUserNotificationCenter,
        shouldPresent notification: NSUserNotification
    ) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
        // 先停 embedding manager (SIGTERM→3s→SIGKILL infinity 子进程,清 runtime/pid|port)
        embeddingManager?.stop(timeoutSec: 4.0)
        // 异步停服务 + 最多等 2s，避免 willTerminate 5s 窗口被卡死后系统强杀
        // 超时也无妨：kb-stop.sh 作孤儿继续跑完；kb-start.sh 启动时会强杀僵尸进程兜底
        let sem = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .userInitiated).async {
            _ = self.runScript("kb-stop.sh")
            sem.signal()
        }
        _ = sem.wait(timeout: .now() + 2.0)
    }

    /// kb-api 第一次健康时拉起 embedding manager(只拉一次)。
    /// 失败容忍:用户仍可用关键词检索,embedding 是可选能力。
    private func ensureEmbeddingManagerStarted() {
        if embeddingStarted { return }
        let mgr = buildDefaultEmbeddingManager(
            dataRoot: projectRoot,
            kbApiPort: resolvePort()
        )
        mgr.start()
        embeddingManager = mgr
        embeddingStarted = true
    }

    private func setupStatusBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = statusItem.button else { return }
        button.image = icon(named: "menu-stopped-64", fallbackSymbol: "stop.circle.fill")
        button.image?.isTemplate = false
        button.image?.size = NSSize(width: 18, height: 18)

        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.addItem(makeMenuItem("打开知识库工作台", action: #selector(openKnowledgeHome), symbol: "house"))
        menu.addItem(makeMenuItem("打开 API 文档", action: #selector(openApiDocs), symbol: "doc.text"))
        menu.addItem(.separator())

        startItem = makeMenuItem("启动知识库", action: #selector(startKnowledgeBase), symbol: "play.circle")
        stopItem = makeMenuItem("停止知识库", action: #selector(stopKnowledgeBase), symbol: "stop.circle")
        embeddingStartItem = makeMenuItem("▶ 启动 Embedding 服务", action: #selector(startEmbeddingService), symbol: "sparkles")
        embeddingStartItem.isEnabled = false  // 默认置灰,refreshEmbeddingMenuState 按实际状态启用
        let statusItemMenu = makeMenuItem("查看状态", action: #selector(showStatus), symbol: "info.circle")
        menu.addItem(startItem)
        menu.addItem(stopItem)
        menu.addItem(embeddingStartItem)
        menu.addItem(statusItemMenu)
        menu.addItem(.separator())

        let kbManage = makeMenuItem("知识库管理", action: nil, symbol: "folder")
        let kbManageSub = NSMenu()
        kbManageSub.addItem(makeMenuItem("导入知识包", action: #selector(importPackage), symbol: "square.and.arrow.down"))
        kbManageSub.addItem(makeMenuItem("导出知识包", action: #selector(exportPackage), symbol: "square.and.arrow.up"))
        kbManageSub.addItem(makeMenuItem("增量导入", action: #selector(incrementalImport), symbol: "plus.square.on.square"))
        kbManageSub.addItem(makeMenuItem("清空知识库", action: #selector(clearKnowledgeBase), symbol: "trash"))
        kbManageSub.addItem(makeMenuItem("清理过期知识", action: #selector(cleanExpiredKnowledge), symbol: "clock.arrow.circlepath"))
        kbManageSub.addItem(.separator())
        kbManageSub.addItem(makeMenuItem("重建向量索引", action: #selector(rebuildVectorIndex), symbol: "arrow.triangle.2.circlepath"))
        kbManageSub.items.forEach { $0.target = self }
        kbManage.submenu = kbManageSub
        menu.addItem(kbManage)
        menu.addItem(.separator())

        menu.addItem(makeMenuItem("退出", action: #selector(quitApp), symbol: "power", keyEquivalent: "q"))

        menu.items.forEach { $0.target = self }
        statusItem.menu = menu
        setState(.stopped)
    }

    private func makeMenuItem(_ title: String, action: Selector?, symbol: String, keyEquivalent: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: keyEquivalent)
        let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: title)?
            .withSymbolConfiguration(cfg) {
            img.isTemplate = true   // 跟随明暗主题 + 选中态自动反色
            item.image = img
        }
        return item
    }

    private func icon(named: String, fallbackSymbol: String) -> NSImage? {
        if let path = Bundle.main.path(forResource: named, ofType: "png"),
           let fromFile = NSImage(contentsOfFile: path) {
            return fromFile
        }
        if let fromBundle = Bundle.main.image(forResource: named) {
            return fromBundle
        }
        return NSImage(systemSymbolName: fallbackSymbol, accessibilityDescription: nil)
    }

    private func setState(_ state: ServiceState) {
        serviceState = state
        guard let button = statusItem.button else { return }
        switch state {
        case .running:
            button.image = icon(named: "menu-running-64", fallbackSymbol: "play.circle.fill")
            startItem?.isEnabled = false
            stopItem?.isEnabled = true
        case .stopped:
            button.image = icon(named: "menu-stopped-64", fallbackSymbol: "stop.circle.fill")
            startItem?.isEnabled = true
            stopItem?.isEnabled = false
        case .busy:
            button.image = icon(named: "menu-busy-64", fallbackSymbol: "arrow.triangle.2.circlepath")
            startItem?.isEnabled = false
            stopItem?.isEnabled = false
        }
        button.image?.isTemplate = false
        button.image?.size = NSSize(width: 18, height: 18)
    }

    private func resolveProjectRoot() -> String {
        guard let path = Bundle.main.path(forResource: "project_root", ofType: "txt"),
              let content = try? String(contentsOfFile: path, encoding: .utf8) else {
            return "/Applications/KnowledgeBase"
        }
        let trimmed = content.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "/Applications/KnowledgeBase" : trimmed
    }

    private func resolvePort() -> Int {
        let configPath = "\(projectRoot)/config/config.toml"
        guard let content = try? String(contentsOfFile: configPath, encoding: .utf8) else {
            return 18000
        }

        var inServer = false
        for rawLine in content.components(separatedBy: .newlines) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("[") {
                inServer = (line == "[server]")
                continue
            }
            guard inServer else { continue }
            if line.hasPrefix("port") {
                let parts = line.split(separator: "=", maxSplits: 1).map { String($0).trimmingCharacters(in: .whitespaces) }
                if parts.count == 2, let p = Int(parts[1]), p > 0 {
                    return p
                }
            }
        }
        return 18000
    }

    private func openURL(_ urlString: String) {
        guard let url = URL(string: urlString) else { return }
        NSWorkspace.shared.open(url)
    }

    private func runScript(_ name: String, args: [String] = []) -> (code: Int32, out: String, err: String) {
        let scriptPath = "\(projectRoot)/scripts/\(name)"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")

        var cmd = "\"\(scriptPath)\""
        if !args.isEmpty {
            cmd += " " + args.map { "\"\($0.replacingOccurrences(of: "\"", with: "\\\\\""))\"" }.joined(separator: " ")
        }
        process.arguments = ["-lc", cmd]

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        process.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        do {
            try process.run()
            process.waitUntilExit()
            let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            let out = String(data: outData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let err = String(data: errData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return (process.terminationStatus, out, err)
        } catch {
            return (1, "", "\(error)")
        }
    }

    private func runOperation(script: String, args: [String] = [], okTitle: String, failTitle: String, autoRefresh: Bool = true) {
        setState(.busy)
        DispatchQueue.global(qos: .userInitiated).async {
            let result = self.runScript(script, args: args)
            DispatchQueue.main.async {
                self.refreshStatus()
                if result.code == 0 {
                    self.notify(title: okTitle, message: result.out.isEmpty ? "完成" : result.out)
                } else {
                    self.notify(title: failTitle, message: [result.out, result.err].filter { !$0.isEmpty }.joined(separator: "\n"))
                }
            }
        }
    }

    private func notify(title: String, message: String) {
        let n = NSUserNotification()
        n.title = title
        n.informativeText = message
        NSUserNotificationCenter.default.deliver(n)
    }

    private func chooseFile(allowedExtensions: [String], title: String) -> String? {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedFileTypes = allowedExtensions
        panel.prompt = "选择"
        panel.message = title
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    private func chooseDirectory(title: String) -> String? {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "选择"
        panel.message = title
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    private func promptIncrementalArgs() -> (project: String, domain: String, knowledgeType: String)? {
        let alert = NSAlert()
        alert.messageText = "增量导入参数"
        alert.informativeText = "请输入项目名，域名和知识类型可选。"
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 320, height: 84))
        let projectField = NSTextField(frame: NSRect(x: 0, y: 56, width: 320, height: 24))
        let domainField = NSTextField(frame: NSRect(x: 0, y: 28, width: 320, height: 24))
        let typeField = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))

        projectField.placeholderString = "project（必填）"
        domainField.placeholderString = "domain（默认 work）"
        typeField.placeholderString = "knowledge_type（默认 fact）"

        container.addSubview(projectField)
        container.addSubview(domainField)
        container.addSubview(typeField)
        alert.accessoryView = container

        let response = alert.runModal()
        guard response == .alertFirstButtonReturn else { return nil }

        let project = projectField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !project.isEmpty else {
            notify(title: "增量导入失败", message: "project 不能为空")
            return nil
        }
        let domainRaw = domainField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let typeRaw = typeField.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let domain = domainRaw.isEmpty ? "work" : domainRaw
        let knowledgeType = typeRaw.isEmpty ? "fact" : typeRaw
        return (project, domain, knowledgeType)
    }

    @objc private func openKnowledgeHome() {
        openURL("http://localhost:\(resolvePort())/console")
    }

    @objc private func openApiDocs() {
        openURL("http://localhost:\(resolvePort())/docs")
    }

    @objc private func startKnowledgeBase() {
        runOperation(script: "kb-start.sh", okTitle: "知识库已启动", failTitle: "启动失败")
    }

    @objc private func stopKnowledgeBase() {
        runOperation(script: "kb-stop.sh", okTitle: "知识库已停止", failTitle: "停止失败")
    }

    @objc private func showStatus() {
        let result = runScript("kb-status.sh")
        let text = [result.out, result.err].filter { !$0.isEmpty }.joined(separator: "\n")
        notify(title: "知识库状态", message: text.isEmpty ? "无状态输出" : text)
        refreshStatus()
    }

    @objc private func importPackage() {
        guard let packagePath = chooseFile(
            allowedExtensions: [
                "tar.gz", "tgz",
                "md", "markdown", "txt",
                "docx", "pdf",
            ],
            title: "选择要导入的文件（.tar.gz 备份包 / .md / .txt / .docx / .pdf）"
        ) else { return }
        runOperation(script: "kb-import-package.sh", args: [packagePath], okTitle: "导入完成", failTitle: "导入失败")
    }
    @objc private func exportPackage() { runOperation(script: "kb-export-package.sh", okTitle: "导出完成", failTitle: "导出失败") }
    @objc private func incrementalImport() {
        guard let dir = chooseDirectory(title: "选择增量导入目录") else { return }
        guard let args = promptIncrementalArgs() else { return }
        runOperation(
            script: "kb-import-incremental.sh",
            args: [dir, args.project, args.domain, args.knowledgeType],
            okTitle: "增量导入完成",
            failTitle: "增量导入失败"
        )
    }
    @objc private func clearKnowledgeBase() { runOperation(script: "kb-clear.sh", okTitle: "已清空知识库", failTitle: "清空失败") }
    @objc private func cleanExpiredKnowledge() { runOperation(script: "kb-clean-expired.sh", okTitle: "已清理过期知识", failTitle: "清理失败") }

    /// 手动启动 Embedding 服务(对齐 Win 侧 tray_app_local.py:_on_start_embedding)。
    /// UX 对齐 Win:菜单项条件 = installed=true + running=false 时可点;
    /// 点击后走 manualStart(config → probe → POST /start,失败 fallback /install repair)。
    /// 期间菜单项置灰 + 标签追加"（启动中）"防连点,completion 回主线程弹通知 + refresh 状态。
    @objc private func startEmbeddingService() {
        guard let mgr = embeddingManager else {
            notify(title: "启动 Embedding 失败", message: "壳层 manager 未初始化:请先启动知识库,等 kb-api 健康后再试")
            return
        }
        if embeddingManualStartInFlight { return }
        embeddingManualStartInFlight = true
        embeddingStartItem.isEnabled = false
        embeddingStartItem.title = "▶ 启动 Embedding 服务（启动中…）"

        mgr.manualStart { [weak self] result in
            guard let self = self else { return }
            self.embeddingManualStartInFlight = false
            self.embeddingStartItem.title = "▶ 启动 Embedding 服务"
            switch result {
            case .success:
                self.notify(
                    title: "Embedding 服务启动指令已下发",
                    message: "warmup 通常 30-60 秒,菜单栏图标翻绿后即可用语义检索。"
                )
            case .failure(let error):
                self.notify(title: "启动 Embedding 失败", message: error.localizedDescription)
            }
            // 触发一次状态刷新,让菜单项按新 embedding 状态置灰/启用
            self.refreshEmbeddingMenuState()
        }
    }

    /// 拉 kb-api /v1/system/embedding-service/status,按 Win UX 判定 embeddingStartItem:
    ///   installed=true + running=false + !warming_up + !inFlight → 可点
    ///   其他 → 置灰
    /// 独立于 kb-api 主服务状态(即使 kb-api 停机也能读到"unknown"→置灰)。
    private func refreshEmbeddingMenuState() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let port = self.resolvePort()
            let canStart = self.probeEmbeddingCanManualStart(port: port)
            DispatchQueue.main.async {
                guard let item = self.embeddingStartItem else { return }
                if self.embeddingManualStartInFlight {
                    item.isEnabled = false
                    return
                }
                item.isEnabled = canStart
            }
        }
    }

    /// 同步(用 semaphore)拉一次 embedding-service/status,判定手动启动是否可用。
    /// 4s 超时;拉不到时保守置灰,避免菜单项显 enable 但点了报错。
    private func probeEmbeddingCanManualStart(port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/v1/system/embedding-service/status") else {
            return false
        }
        var req = URLRequest(url: url)
        req.httpMethod = "GET"
        req.timeoutInterval = 4.0

        let sem = DispatchSemaphore(value: 0)
        var body: Data?
        URLSession.shared.dataTask(with: req) { data, _, _ in
            body = data
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 5.0)

        guard let data = body,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return false
        }
        let installed = obj["installed"] as? Bool ?? false
        let running = obj["running"] as? Bool ?? false
        let warmingUp = obj["warming_up"] as? Bool ?? false
        return installed && !running && !warmingUp
    }

    /// 触发全量向量索引重建（strict mode）：清空 qdrant collection → 流式 embed
    /// 所有 active chunk → 回写 vector_id。期间维护标志置位，写类 API 返 202。
    /// 二次确认 + 后端 confirm token（I-CONFIRM-OVERWRITE）防误触。
    @objc private func rebuildVectorIndex() {
        let alert = NSAlert()
        alert.messageText = "重建向量索引"
        alert.informativeText = """
        将清空现有向量集合并重新走 embedding 算所有 chunk 的向量。
        - 切换 embedding 模型 / 维度后必须重建
        - 期间不能写入知识库（自动维护态）
        - 时长视 chunk 数与模型而定：~1100 chunk · CPU bge-m3 约 1-3 分钟
        进度可在「打开知识库工作台 → 设置」中实时查看。
        """
        alert.alertStyle = .warning
        alert.addButton(withTitle: "开始重建")
        alert.addButton(withTitle: "取消")
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let port = resolvePort()
        guard let url = URL(string: "http://127.0.0.1:\(port)/v1/system/rebuild-vector-index") else {
            notify(title: "重建启动失败", message: "kb-api 端口未就绪")
            return
        }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 10
        req.httpBody = "{\"confirm\":\"I-CONFIRM-OVERWRITE\",\"batch_size\":100}".data(using: .utf8)

        URLSession.shared.dataTask(with: req) { [weak self] data, response, err in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let err = err {
                    self.notify(title: "重建启动失败", message: "网络错误：\(err.localizedDescription)")
                    return
                }
                guard let http = response as? HTTPURLResponse else {
                    self.notify(title: "重建启动失败", message: "无 HTTP 响应")
                    return
                }
                let bodyText = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                switch http.statusCode {
                // 200 = 同步完成（小批量秒回）；202 = 异步启动（rebuild_runner 后台线程）。
                // 都视为"成功进入 rebuild 状态"，差别仅在前端要不要 poll；这里统一 poll。
                case 200, 202:
                    self.notify(
                        title: "向量索引重建已启动",
                        message: "后台进行中，跑完会弹通知。也可在「打开知识库工作台 → 设置」实时看进度。"
                    )
                    self.pollRebuildStatusUntilDone(port: port)
                case 409:
                    self.notify(title: "已有重建在跑", message: "请等当前 rebuild 完成或先到设置页面 abort")
                case 400:
                    // 典型场景：embedding 是 HashEmbedding 兜底（mode=disabled 或外部 API 不可达）
                    self.notify(
                        title: "重建无法启动",
                        message: "请先在「设置」页配置可用的 embedding 服务。详情：\(String(bodyText.prefix(160)))"
                    )
                case 503:
                    // embedding 服务还没就绪（infinity 装机后 warmup 30-60s 才 LISTEN）。
                    // 后端拒在进 RUNNING 之前，旧索引完好；提示用户等 banner ready。
                    self.notify(
                        title: "embedding 服务尚未就绪",
                        message: "请等托盘 banner 显示 ready 后再点重建（通常装机后 30-60 秒）。"
                    )
                default:
                    self.notify(
                        title: "重建启动失败",
                        message: "HTTP \(http.statusCode)：\(String(bodyText.prefix(200)))"
                    )
                }
            }
        }.resume()
    }

    /// 后台轮询 rebuild status，跑到 completed / failed 时弹通知闭环。
    /// 节奏：3s 一次，最长 30 分钟兜底（避免极端长任务下挂死）。
    /// 单实例保护：rebuildPollerActive 防止用户连点托盘菜单触发多个 poller 并发刷屏。
    private func pollRebuildStatusUntilDone(port: Int) {
        if rebuildPollerActive { return }
        rebuildPollerActive = true

        guard let statusURL = URL(string: "http://127.0.0.1:\(port)/v1/system/rebuild-vector-index/status") else {
            rebuildPollerActive = false
            return
        }
        let pollIntervalSec: TimeInterval = 3.0
        let maxDurationSec: TimeInterval = 30 * 60
        let startedAt = Date()

        DispatchQueue.global(qos: .utility).async { [weak self] in
            while true {
                guard let self = self else { return }
                if Date().timeIntervalSince(startedAt) > maxDurationSec {
                    DispatchQueue.main.async {
                        self.notify(
                            title: "重建状态轮询超时",
                            message: "已超 30 分钟未收到完成信号；请到「设置」页确认 rebuild 状态。"
                        )
                        self.rebuildPollerActive = false
                    }
                    return
                }
                Thread.sleep(forTimeInterval: pollIntervalSec)

                // 同步拉一次 status（用 semaphore 把 async data task 转同步，保持 polling 循环简单）
                let sem = DispatchSemaphore(value: 0)
                var responseData: Data?
                URLSession.shared.dataTask(with: statusURL) { data, _, _ in
                    responseData = data
                    sem.signal()
                }.resume()
                _ = sem.wait(timeout: .now() + 5.0)

                guard let data = responseData,
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let status = obj["status"] as? String else {
                    continue  // 拉空 / 解析失败 → 不报错，下一轮重试（kb-api 临时抖动可恢复）
                }

                if status == "completed" {
                    let processed = obj["processed"] as? Int ?? 0
                    let total = obj["total"] as? Int ?? 0
                    let elapsed = Int(Date().timeIntervalSince(startedAt))
                    DispatchQueue.main.async {
                        self.notify(
                            title: "向量索引重建完成",
                            message: "处理 \(processed)/\(total) chunk，用时 \(elapsed) 秒。语义检索已生效，可直接问答。"
                        )
                        self.rebuildPollerActive = false
                    }
                    return
                }
                if status == "failed" {
                    let errText = String((obj["error"] as? String ?? "").prefix(220))
                    DispatchQueue.main.async {
                        self.notify(
                            title: "向量索引重建失败",
                            message: errText.isEmpty ? "未知错误，请查看「设置」页详情" : errText
                        )
                        self.rebuildPollerActive = false
                    }
                    return
                }
                // running / idle / 其他过渡态：继续 poll
            }
        }
    }

    @objc private func quitApp() { NSApp.terminate(nil) }

    private func refreshStatus() {
        DispatchQueue.global(qos: .utility).async {
            let result = self.runScript("kb-status.sh", args: ["--short"])
            let text = result.out.lowercased()
            DispatchQueue.main.async {
                if result.code == 0 && text.contains("running") {
                    self.setState(.running)
                    // kb-api 一旦健康就拉起 embedding 壳层 manager (只拉一次)
                    self.ensureEmbeddingManagerStarted()
                } else {
                    self.setState(.stopped)
                }
                // 无论 kb-api 状态如何都刷新 embedding 菜单项(kb 停机时 probe 拉空 → 置灰)
                self.refreshEmbeddingMenuState()
            }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
