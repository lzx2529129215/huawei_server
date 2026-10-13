from pathlib import Path

from memscenes.kernel_capture import DeviceDmesgReader


class DeviceStub:
    def __init__(self):
        self.commands = []

    def shell(self, command, **kwargs):
        self.commands.append(command)
        return ""


def handle():
    reader = object.__new__(DeviceDmesgReader)
    reader.pid, reader.start_ticks, reader.comm = 123, 456, "nohup"
    reader.returncode = None
    reader.device = DeviceStub()
    return reader


def test_reused_pid_is_never_signalled():
    reader = handle()
    reader._identity = lambda: {"start_ticks": 999, "comm": "nohup", "state": "S"}
    reader.terminate()
    assert reader.returncode == -1
    assert not reader.device.commands


def test_missing_reader_does_not_report_success():
    reader = handle()
    reader._identity = lambda: None
    assert reader.poll() == -1


def test_signal_contains_device_side_identity_check():
    reader = handle()
    reader._identity = lambda: {"start_ticks": 456, "comm": "nohup", "state": "S"}
    reader.terminate()
    command, = reader.device.commands
    assert '${22}' in command and "'456'" in command
    assert "'(nohup)'" in command and 'kill -15 123' in command


def test_collect_preserves_segments_and_does_not_duplicate_refresh(tmp_path):
    reader = handle()
    reader.index = 1
    reader.raw_path, reader.stderr_path = 'remote_raw', 'remote_stderr'
    (tmp_path/'kernel_segment_0000.raw.log').write_bytes(b'first\n')
    def receive(remote, local):
        Path(local).write_bytes(b'second\n' if remote == 'remote_raw' else b'')
    reader.device.receive = receive
    for _ in range(2):
        reader.collect(tmp_path/'kernel_dmesg.raw.log', tmp_path/'stderr.log')
        assert (tmp_path/'kernel_dmesg.raw.log').read_bytes() == b'first\nsecond\n'
