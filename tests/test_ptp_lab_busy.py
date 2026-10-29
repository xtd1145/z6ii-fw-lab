# SPDX-License-Identifier: GPL-2.0-or-later
"""A cleanup attempt must preserve the packet of an unfinished native call."""
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.z62_ptp_lab import NativePtp


class BusyRemote:
    def __init__(self): self.writes = []
    def select_thread(self, thread): pass
    def read(self, address, length): return (2).to_bytes(4, 'little')
    def write(self, address, data): self.writes.append((address, data))


with tempfile.TemporaryDirectory() as folder:
    remote = BusyRemote()
    adapter = NativePtp(remote, dict(mailbox=0x1000, packet=0x2000), Path(folder))
    adapter.responses = [(0x2001, [0x2001])]
    try:
        adapter.transaction(0x94fe, [0xf002, 0, 0, 0, 0])
    except RuntimeError as error:
        assert 'packet preserved' in str(error)
    else:
        raise AssertionError('Busy original operation accepted another packet')
    assert not remote.writes
    assert adapter.responses == [(0x2001, [0x2001])]
    assert adapter.transaction_id == 0
    assert not list(Path(folder).iterdir())
print('PASS unfinished native operation preserves its packet, response and transaction identity')
