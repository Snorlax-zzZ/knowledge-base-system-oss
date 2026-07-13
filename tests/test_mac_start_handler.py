"""编译并执行 Swift StartHandler 的早期句柄发布行为测试。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SWIFT_SOURCE = ROOT / "mac-app" / "MenuBarApp" / "EmbeddingProcessManager.swift"


@pytest.mark.skipif(sys.platform != "darwin", reason="Swift 行为测试仅在 macOS 可用")
def test_post_install_accepts_sse_response_headers_without_waiting_for_stream(tmp_path):
    """install desired 在响应头发出前已落地，客户端不得等待/解析长 SSE 正文。"""
    if shutil.which("xcrun") is None:
        pytest.skip("xcrun 不可用")

    harness = tmp_path / "InstallSseHeadersHarness.swift"
    binary = tmp_path / "install-sse-headers-harness"
    harness.write_text(
        r'''
import Foundation

final class HeadersThenTimeoutProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "text/event-stream"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        // 模拟 SSE 连接在 headers 后长期不结束。旧 JSON client 会把后续 timeout
        // 当 POST 失败；header-aware command client 应已返回成功。
        client?.urlProtocol(self, didFailWithError: URLError(.timedOut))
    }

    override func stopLoading() {}
}

@main
struct InstallSseHeadersHarness {
    static func main() {
        let root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("kb-install-sse-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(
            at: root, withIntermediateDirectories: true, attributes: nil
        )
        defer { try? FileManager.default.removeItem(at: root) }
        let tokenPath = root.appendingPathComponent("owner_token")
        try? "test-token".write(to: tokenPath, atomically: true, encoding: .utf8)

        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [HeadersThenTimeoutProtocol.self]
        let session = URLSession(configuration: config)
        let client = KbApiClient(
            baseURL: URL(string: "http://127.0.0.1:18000")!,
            tokenSource: OwnerTokenSource(
                path: tokenPath, bootTimeoutSec: 1.0, pollIntervalSec: 0.01
            ),
            session: session
        )

        do {
            try client.postInstall(modelId: "bge-m3", device: "cpu")
        } catch {
            fputs("postInstall waited for SSE body and failed: \(error)\n", stderr)
            exit(2)
        }
    }
}
''',
        encoding="utf-8",
    )

    compiled = subprocess.run(
        ["xcrun", "swiftc", str(SWIFT_SOURCE), str(harness), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    executed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=8)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Swift 行为测试仅在 macOS 可用")
def test_start_handler_publishes_process_before_warmup_wait(tmp_path):
    if shutil.which("xcrun") is None:
        pytest.skip("xcrun 不可用")

    harness = tmp_path / "StartHandlerHarness.swift"
    binary = tmp_path / "start-handler-harness"
    harness.write_text(
        r'''
import Foundation

@main
struct StartHandlerHarness {
    static func main() {
        let root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("kb-start-handler-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(
            at: root, withIntermediateDirectories: true, attributes: nil
        )
        defer { try? FileManager.default.removeItem(at: root) }

        let spec = StartSpec(
            modelId: "test-model",
            device: "cpu",
            startCmd: ["/bin/sleep", "5"],
            port: 65534,
            runtimeDir: root.appendingPathComponent("runtime"),
            infinityLogPath: root.appendingPathComponent("infinity.log"),
            env: [:]
        )
        let handler = StartHandler(warmupTimeoutSec: 3.0, probeIntervalSec: 0.1)
        let published = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let handleLock = NSLock()
        var spawned: InfinityProcess?

        DispatchQueue.global(qos: .utility).async {
            _ = handler.spawnAndWaitReady(spec, onSpawn: { handle in
                handleLock.lock()
                spawned = handle
                handleLock.unlock()
                published.signal()
            })
            finished.signal()
        }

        guard published.wait(timeout: .now() + 1.0) == .success else {
            fputs("spawn handle was not published before warmup wait\n", stderr)
            exit(2)
        }
        handleLock.lock()
        let handle = spawned
        handleLock.unlock()
        handle?.terminate()
        _ = finished.wait(timeout: .now() + 2.0)
    }
}
''',
        encoding="utf-8",
    )

    compiled = subprocess.run(
        ["xcrun", "swiftc", str(SWIFT_SOURCE), str(harness), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    executed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=8)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Swift 行为测试仅在 macOS 可用")
def test_adopted_process_handle_terminates_only_the_owned_process(tmp_path):
    if shutil.which("xcrun") is None:
        pytest.skip("xcrun 不可用")

    harness = tmp_path / "AdoptedHandleHarness.swift"
    binary = tmp_path / "adopted-handle-harness"
    harness.write_text(
        r'''
import Foundation

@main
struct AdoptedHandleHarness {
    static func main() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = [
            "-c", "import time; time.sleep(20)",
            "infinity", "--port", "7687", "--model-id", "bge-m3"
        ]
        do {
            try proc.run()
        } catch {
            fputs("fixture spawn failed: \(error)\n", stderr)
            exit(2)
        }
        defer {
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        }
        Thread.sleep(forTimeInterval: 0.1)

        let handle = InfinityProcess(
            adoptedPid: proc.processIdentifier,
            expectedModelId: "bge-m3",
            expectedPort: 7687
        )
        guard handle.isRunning else {
            fputs("owned adopted process was not recognized\n", stderr)
            exit(3)
        }
        let stopper = StopHandler(graceSec: 1.0, pollIntervalSec: 0.05)
        let runtime = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("kb-adopted-stop-\(UUID().uuidString)")
        _ = stopper.terminateAndWait(handle, runtimeDir: runtime)
        Thread.sleep(forTimeInterval: 0.1)
        guard !proc.isRunning else {
            fputs("adopted process survived stop\n", stderr)
            exit(4)
        }

        // PID 存活但命令行不属于目标模型/端口时，句柄必须拒绝接管与发信号。
        let unrelated = Process()
        unrelated.executableURL = URL(fileURLWithPath: "/bin/sleep")
        unrelated.arguments = ["20"]
        try? unrelated.run()
        defer {
            if unrelated.isRunning { kill(unrelated.processIdentifier, SIGKILL) }
        }
        let rejected = InfinityProcess(
            adoptedPid: unrelated.processIdentifier,
            expectedModelId: "bge-m3",
            expectedPort: 7687
        )
        guard !rejected.isRunning else {
            fputs("unrelated PID was treated as owned\n", stderr)
            exit(5)
        }
        rejected.terminate()
        Thread.sleep(forTimeInterval: 0.1)
        guard unrelated.isRunning else {
            fputs("unrelated process was terminated\n", stderr)
            exit(6)
        }
    }
}
''',
        encoding="utf-8",
    )

    compiled = subprocess.run(
        ["xcrun", "swiftc", str(SWIFT_SOURCE), str(harness), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    executed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=8)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Swift 行为测试仅在 macOS 可用")
def test_stop_handler_reports_a_stable_final_result(tmp_path):
    if shutil.which("xcrun") is None:
        pytest.skip("xcrun 不可用")

    harness = tmp_path / "StopResultHarness.swift"
    binary = tmp_path / "stop-result-harness"
    harness.write_text(
        r'''
import Foundation

@main
struct StopResultHarness {
    static func main() {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["20"]
        try? proc.run()
        defer {
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        }

        let handle = InfinityProcess(process: proc)
        let runtime = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("kb-stop-result-\(UUID().uuidString)")
        let result = StopHandler(
            graceSec: 1.0,
            pollIntervalSec: 0.05
        ).terminateAndWait(handle, runtimeDir: runtime)
        guard result.stopped else {
            fputs("stop handler did not report the observed final state\n", stderr)
            exit(2)
        }
        guard result.lastError.isEmpty else {
            fputs("successful stop unexpectedly returned an error\n", stderr)
            exit(3)
        }
    }
}
''',
        encoding="utf-8",
    )

    compiled = subprocess.run(
        ["xcrun", "swiftc", str(SWIFT_SOURCE), str(harness), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    executed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=8)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Swift 行为测试仅在 macOS 可用")
def test_adopted_process_accepts_equals_style_cli_options(tmp_path):
    if shutil.which("xcrun") is None:
        pytest.skip("xcrun 不可用")

    harness = tmp_path / "EqualsStyleAdoptHarness.swift"
    binary = tmp_path / "equals-style-adopt-harness"
    harness.write_text(
        r'''
import Foundation

@main
struct EqualsStyleAdoptHarness {
    static func main() {
        let root = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("kb-equals-adopt-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(
            at: root, withIntermediateDirectories: true, attributes: nil
        )
        defer { try? FileManager.default.removeItem(at: root) }

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        proc.arguments = [
            "-c", "import time; time.sleep(20)",
            "infinity", "--port=7688", "--model-id=eq-model"
        ]
        try? proc.run()
        defer {
            if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        }
        Thread.sleep(forTimeInterval: 0.1)
        try? "\(proc.processIdentifier)".write(
            to: root.appendingPathComponent("pid"),
            atomically: true, encoding: .utf8
        )
        try? "7688".write(
            to: root.appendingPathComponent("port"),
            atomically: true, encoding: .utf8
        )

        let cleaner = StaleResidueCleaner(runtimeDir: root)
        let (pid, port) = cleaner.adoptOrClean(expectedModelId: "eq-model")
        guard pid == proc.processIdentifier, port == 7688 else {
            kill(proc.processIdentifier, SIGKILL)
            fputs("cleaner rejected equals-style owned command line\n", stderr)
            exit(2)
        }

        let handle = InfinityProcess(
            adoptedPid: proc.processIdentifier,
            expectedModelId: "eq-model",
            expectedPort: 7688
        )
        guard handle.isRunning else {
            kill(proc.processIdentifier, SIGKILL)
            fputs("adopted handle rejected equals-style owned command line\n", stderr)
            exit(3)
        }
        _ = StopHandler(
            graceSec: 1.0,
            pollIntervalSec: 0.05
        ).terminateAndWait(handle, runtimeDir: root)
        Thread.sleep(forTimeInterval: 0.1)
        guard !proc.isRunning else {
            kill(proc.processIdentifier, SIGKILL)
            fputs("equals-style adopted process survived stop\n", stderr)
            exit(4)
        }
    }
}
''',
        encoding="utf-8",
    )

    compiled = subprocess.run(
        ["xcrun", "swiftc", str(SWIFT_SOURCE), str(harness), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    executed = subprocess.run([str(binary)], capture_output=True, text=True, timeout=8)
    assert executed.returncode == 0, executed.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="Swift 行为测试仅在 macOS 可用")
def test_install_executor_runs_venv_probe_from_embedding_service_directory(tmp_path):
    if shutil.which("xcrun") is None:
        pytest.skip("xcrun 不可用")

    service_dir = tmp_path / "embedding-service"
    venv_python = service_dir / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(
        "#!/bin/sh\n"
        f'if [ "$PWD" != "{service_dir}" ]; then exit 42; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    venv_python.chmod(0o755)

    model_dir = service_dir / "models" / "bge-m3"
    model_dir.mkdir(parents=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    with (model_dir / "model.safetensors").open("wb") as weight_file:
        weight_file.truncate(51 * 1024 * 1024)

    harness = tmp_path / "VenvProbeCwdHarness.swift"
    binary = tmp_path / "venv-probe-cwd-harness"
    harness.write_text(
        r'''
import Foundation

@main
struct VenvProbeCwdHarness {
    static func main() {
        let serviceDir = URL(fileURLWithPath: "__SERVICE_DIR__")
        let modelDir = URL(fileURLWithPath: "__MODEL_DIR__")
        let runtimeDir = serviceDir.appendingPathComponent("runtime")
        let installer = InstallExecutor(
            statusWriter: InstallStatusWriter(
                path: runtimeDir.appendingPathComponent("install_status.json")
            ),
            pipLogPath: serviceDir.appendingPathComponent("pip.log")
        )
        let spec = InstallSpec(
            modelId: "bge-m3",
            venvDir: serviceDir.appendingPathComponent("venv").path,
            modelDir: modelDir.path,
            device: "cpu",
            createVenvCmd: ["/usr/bin/false"],
            pipInstallCmd: ["/usr/bin/false"],
            downloadArgs: [
                "repo_id": "test/bge-m3",
                "local_dir": modelDir.path,
                "endpoint": "https://invalid.example",
            ],
            mirrorChain: []
        )
        guard installer.execute(spec) else {
            fputs("venv probe inherited the launcher cwd and triggered repair\n", stderr)
            exit(2)
        }
    }
}
'''.replace("__SERVICE_DIR__", str(service_dir))
        .replace("__MODEL_DIR__", str(model_dir)),
        encoding="utf-8",
    )

    compiled = subprocess.run(
        ["xcrun", "swiftc", str(SWIFT_SOURCE), str(harness), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr

    executed = subprocess.run(
        [str(binary)], cwd="/", capture_output=True, text=True, timeout=8
    )
    assert executed.returncode == 0, executed.stderr
