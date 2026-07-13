"""Mac Embedding manager 的队列隔离回归测试。"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWIFT_SOURCE = ROOT / "mac-app" / "MenuBarApp" / "EmbeddingProcessManager.swift"


def _method_block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_auto_bootstrap_does_not_enqueue_behind_permanent_reconcile_loop():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert 'DispatchQueue(label: "embed.commands"' in source
    block = _method_block(
        source,
        "private func maybeAutoBootstrap()",
        "/// 磁盘兜底探测",
    )
    assert "commandQueue.async" in block
    assert "workQueue.async" not in block


def test_manual_start_uses_the_same_serial_command_queue():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(
        source,
        "public func manualStart",
        "/// auto-bootstrap 与手动触发共用的核心 handshake",
    )
    assert "commandQueue.async" in block
    assert "workQueue.async" not in block


def test_reconcile_queue_has_only_the_permanent_loop_submission():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert source.count("workQueue.async") == 1


def test_start_publishes_warming_state_before_waiting_for_health():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    warming = block.index("actual.warmingUp = true")
    published = block.index("writeActual(", warming)
    blocking_wait = block.index("starter.spawnAndWaitReady")
    assert warming < published < blocking_wait


def test_auto_bootstrap_only_runs_while_database_mode_is_local():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(
        source,
        "private func maybeAutoBootstrap()",
        "/// 磁盘兜底探测",
    )
    mode_read = block.index("client.getSystemConfig()")
    mode_guard = block.index('mode == "local"')
    scheduled = block.index("commandQueue.async")
    assert mode_read < mode_guard < scheduled


def test_failed_auto_bootstrap_rearms_a_later_retry():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(
        source,
        "private func triggerAutoBootstrapStart",
        "/// UI 手动触发入口",
    )
    assert "case .failure" in block
    assert "resetAutoBootstrapAttempt" in block


def test_start_registers_spawned_handle_before_blocking_warmup_wait():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    call = block.index("starter.spawnAndWaitReady")
    callback = block.index("onSpawn:", call)
    registered = block.index("currentHandle = spawned", callback)
    stop_guard = block.index("isStopping()", registered)
    assert call < callback < registered < stop_guard


def test_adopted_process_is_registered_as_a_terminable_handle():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    constructed = block.index("let adoptedHandle = InfinityProcess(")
    adopted_pid = block.index("adoptedPid: pid", constructed)
    registered = block.index("currentHandle = adoptedHandle", adopted_pid)
    assert constructed < adopted_pid < registered


def test_adopted_process_is_stopped_if_quit_wins_the_registration_race():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    registered = block.index("currentHandle = adoptedHandle")
    stop_guard = block.index("isStopping()", registered)
    killed = block.index("adoptedHandle.kill()", stop_guard)
    assert registered < stop_guard < killed


def test_adopted_handle_uses_the_runtime_port_validated_by_cleaner():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    returned_port = block.index("let (adoptPid, adoptPort)")
    unwrapped_port = block.index("let port = adoptPort", returned_port)
    handle_port = block.index("expectedPort: port", unwrapped_port)
    actual_port = block.index("actual.port = port", handle_port)
    assert returned_port < unwrapped_port < handle_port < actual_port


def test_spawn_registration_race_uses_immediate_kill_during_app_quit():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    registered = block.index("currentHandle = spawned")
    stop_guard = block.index("isStopping()", registered)
    killed = block.index("spawned.kill()", stop_guard)
    assert registered < stop_guard < killed


def test_failed_dispatch_is_retried_instead_of_acknowledged_once_forever():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    tick = _method_block(source, "private func tick()", "// MARK: - Auto-bootstrap")
    dispatched = tick.index("let completed = dispatch(desired: desired)")
    success_guard = tick.index("if completed", dispatched)
    acknowledged = tick.index("lastDoneGeneration = desired.generation", success_guard)
    failure_branch = tick.index("} else {", acknowledged)
    retry_backoff = tick.index("bumpBackoff()", failure_branch)
    no_ack = tick.index("acknowledgeDesired: false", retry_backoff)
    assert dispatched < success_guard < acknowledged < failure_branch < retry_backoff < no_ack


def test_dispatch_and_start_report_success_or_failure():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    dispatch = _method_block(source, "private func dispatch", "/// 返回 true = install")
    assert "private func dispatch(desired: EmbedDesiredState) -> Bool" in dispatch
    assert "return doStart(" in dispatch

    start = _method_block(source, "private func doStart", "private func doStop")
    assert "private func doStart(" in start
    assert ") -> Bool" in start.split("{", 1)[0]
    assert "return false" in start
    assert "return true" in start


def test_warming_snapshot_does_not_acknowledge_start_before_spawn_succeeds():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    warming = block.index("actual.warmingUp = true")
    write = block.index("writeActual(", warming)
    no_ack = block.index("acknowledgeDesired: false", write)
    spawn = block.index("starter.spawnAndWaitReady", no_ack)
    assert warming < write < no_ack < spawn


def test_probe_repair_marks_install_for_followup_start_before_posting_install():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(
        source,
        "private func performStartHandshake",
        "// MARK: - Self-heal",
    )
    probe_failed = block.index("if !probeOK")
    marked = block.index("setStartAfterInstallRequested(true)", probe_failed)
    install_post = block.index("client.postInstall", marked)
    rollback = block.index("setStartAfterInstallRequested(false)", install_post)
    assert probe_failed < marked < install_post < rollback


def test_successful_repair_install_posts_start_and_transport_failure_retries():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func dispatch", "/// 返回 true = install")
    install_case = block.index('case "install"')
    installed = block.index("doInstall", install_case)
    consumed = block.index("consumeStartAfterInstallRequest()", installed)
    start_post = block.index("client.postStart", consumed)
    expected_generation = block.index("expectedGeneration: desired.generation", start_post)
    rearmed = block.index("setStartAfterInstallRequested(true)", start_post)
    assert install_case < installed < consumed < start_post < expected_generation < rearmed


def test_venv_probe_allows_sixty_seconds_for_cold_imports():
    """实机完整 import 约 21 秒，20 秒预算会把健康 venv 误判成损坏。"""
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(
        source,
        "fileprivate func venvDepsReady",
        "/// 判定规则（保守）",
    )
    assert "timeout: 60.0" in block


def test_failed_warmup_clears_the_handle_published_by_on_spawn():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    block = _method_block(source, "private func doStart", "private func doStop")
    final_state = block.index("if let h = handle")
    failed = block.index("} else {", final_state)
    failure_end = block.index("let succeeded", failed)
    failure_block = block[failed:failure_end]
    assert "currentHandle = nil" in failure_block
    assert "actual.pid = nil" in failure_block


def test_stop_uses_stop_handler_result_without_a_second_process_probe():
    source = SWIFT_SOURCE.read_text(encoding="utf-8")
    assert "struct StopResult" in source
    block = _method_block(source, "private func doStop", "private func maybeHeartbeat")
    assert "let result = stopper.terminateAndWait" in block
    assert "return result.stopped" in block
    assert "return !h.isRunning" not in block
    assert "if result.stopped" in block
    assert "currentHandle = h" in block
