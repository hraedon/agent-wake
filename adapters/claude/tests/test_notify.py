import io
import json
import threading

from agent_wake_claude import _notify


class _RecordingStdout(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1


def test_send_writes_one_json_rpc_line_and_flushes(monkeypatch):
    stdout = _RecordingStdout()
    monkeypatch.setattr(_notify.sys, "stdout", stdout)

    message = {"jsonrpc": "2.0", "method": "notifications/claude/channel"}
    _notify.send(message)

    assert stdout.getvalue().endswith("\n")
    assert json.loads(stdout.getvalue()) == message
    assert stdout.flush_count == 1


def test_send_does_not_write_partial_output_when_serialization_fails(monkeypatch):
    stdout = _RecordingStdout()
    monkeypatch.setattr(_notify.sys, "stdout", stdout)

    try:
        _notify.send({"value": object()})
    except TypeError:
        pass
    else:
        raise AssertionError("non-JSON value should fail serialization")

    assert stdout.getvalue() == ""
    assert stdout.flush_count == 0


def test_concurrent_sends_remain_complete_json_lines(monkeypatch):
    stdout = _RecordingStdout()
    monkeypatch.setattr(_notify.sys, "stdout", stdout)
    threads = [
        threading.Thread(target=_notify.send, args=({"sequence": sequence},))
        for sequence in range(32)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    documents = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert sorted(document["sequence"] for document in documents) == list(range(32))
    assert stdout.flush_count == 32
