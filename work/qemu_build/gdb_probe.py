r"""Drive QEMU's gdbstub directly, because this build has no HMP `watch`.

MSYS2's gdb has no ARM target description, so the usual `target remote` route is
dead, but a write watchpoint is only a `Z,2,addr,len` packet away. This client
starts nikon-z6ii with `-S -gdb`, plants watchpoints on the DDR control-block
cells the boot path misuses as pointers, and reports the exact guest PC that
retires the store.

  python gdb_probe.py --wp 0xc18ad858 --wp 0xc18ad854

VAs are what the guest sees; QEMU matches the virtual page at the access site,
so mas's own page-table view is respected.
"""
import argparse
import socket
import subprocess
import sys
import time

sys.path.insert(0, r"D:\z6ii_fw_lab\work\qemu_build")
from mon_probe import CPU, MAS, QEMU, TRACE, qemu_env  # noqa: E402

GDB_PORT = 1235
REG_NAMES = ["R%d" % n for n in range(16)] + ["CPSR"]


class Stub:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        # QEMU starts in ACK mode; sending '+' once no-ack is negotiated makes
        # it resync and retransmit, which garbles every later reply.
        self.ack = True

    def send(self, payload):
        pkt = ("$%s#%02x" % (payload, sum(payload.encode()) & 0xFF)).encode()
        self.sock.sendall(pkt)

    def recv_packet(self, timeout):
        """Return one $..#.. payload, or None on timeout."""
        end = time.time() + timeout
        depth = 0
        start = 0
        while time.time() < end:
            self.sock.settimeout(max(0.01, end - time.time()))
            try:
                self.buf += self.sock.recv(4096)
            except socket.timeout:
                break
            while True:
                if depth == 0:
                    i = self.buf.find(b"$")
                    if i < 0:
                        self.buf = b""
                        break
                    self.buf = self.buf[i + 1:]
                    depth = 1
                    start = 0
                    continue
                j = self.buf.find(b"#", start)
                if j < 0:
                    start = len(self.buf)
                    break
                if len(self.buf) >= j + 3:
                    payload = self.buf[:j].decode("latin1")
                    self.buf = self.buf[j + 3:]
                    depth = 0
                    if self.ack:
                        self.sock.sendall(b"+")
                    return payload
                start = j + 1
        return None

    def cmd(self, payload, timeout=3.0):
        self.send(payload)
        return self.recv_packet(timeout)

    def noack(self):
        if self.cmd("QStartNoAckMode") == "OK":
            self.ack = False

    def interrupt(self):
        self.sock.sendall(b"\x03")


def regs(text):
    # QEMU answers `g` with bare hex, no leading letter.
    if not text or len(text) < 68 * 2:
        return None
    raw = bytes.fromhex(text[:68 * 2])
    out = {}
    for n, name in enumerate(REG_NAMES):
        out[name] = int.from_bytes(raw[4 * n:4 * n + 4], "little")
    return out


def mem(stub, addr, size):
    text = stub.cmd("m%x,%x" % (addr, size))
    if not text or all(c not in "0123456789abcdef" for c in text):
        return None
    data = bytes.fromhex(text[:len(text) // 2 * 2])
    return [int.from_bytes(data[i:i + 4], "little")
            for i in range(0, len(data) - 3, 4)]


def show(stub, addr, note):
    text = stub.cmd("g", 3.0)
    r = regs(text)
    if r is None:
        print("%s: no register reply (%r)" % (note, text))
        return None
    print("== %s ==" % note)
    print("  PC=0x%08x LR=0x%08x SP=0x%08x CPSR=0x%08x"
          % (r["R15"], r["R14"], r["R13"], r["CPSR"]))
    print("  r0..r9 = " + " ".join("0x%08x" % r["R%d" % n] for n in range(10)))
    print("  r10..r12 = " + " ".join("0x%08x" % r["R%d" % n]
                                     for n in range(10, 13)))
    words = mem(stub, addr, 16)
    if words:
        print("  [0x%08x] = %s" % (addr & ~0xF,
                                   " ".join("0x%08x" % w for w in words)))
    frame = mem(stub, r["R13"], 64)
    if frame:
        print("  sp[0:16] = " + " ".join("0x%08x" % w for w in frame))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wp", action="append", default=[],
                    help="virtual address to plant a write watchpoint on")
    ap.add_argument("--smp", type=int, default=1)
    ap.add_argument("--budget", type=float, default=25.0,
                    help="total seconds of guest execution to allow")
    ap.add_argument("--gran", type=float, default=1.0,
                    help="interrupt and sample the guest this often")
    args = ap.parse_args()

    proc = subprocess.Popen(
        [QEMU, "-M", "nikon-z6ii", "-cpu", CPU, "-smp", str(args.smp),
         "-kernel", MAS, "-d", "int,unimp", "-D", TRACE,
         "-S", "-gdb", "tcp:127.0.0.1:%d,server,nowait" % GDB_PORT,
         "-monitor", "none", "-display", "none"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=qemu_env())

    end = time.time() + 6.0
    sock = None
    while time.time() < end:
        try:
            sock = socket.create_connection(("127.0.0.1", GDB_PORT), 0.3)
            break
        except OSError:
            time.sleep(0.2)
    if sock is None:
        print("gdbstub never came up", file=sys.stderr)
        proc.kill()
        return 1
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    stub = Stub(sock)
    hit = None
    try:
        print(stub.cmd("?"))
        print(stub.cmd("qSupported:multiprocess+;xmlRegisters=arm;qRelocInsn+"))
        stub.noack()
        for addr in args.wp:
            a = int(addr, 16)
            reply = stub.cmd("Z,2,%x,4" % a)
            print("watchpoint %s -> %r" % (addr, reply))
        show(stub, int(args.wp[0], 16) if args.wp else 0, "entry state")
        deadline = time.time() + args.budget
        while time.time() < deadline:
            stub.send("c")
            reply = stub.recv_packet(args.gran)
            if reply is None:
                stub.interrupt()
                reply = stub.recv_packet(3.0)
                if reply is None:
                    print("guest is not answering the interrupt")
                    break
                show(stub, int(args.wp[0], 16) if args.wp else 0,
                     "sampled, %.1fs left" % (deadline - time.time()))
                continue
            if "watch" in reply:
                print("reply: %s" % reply)
                hit = show(stub, int(args.wp[0], 16), "watchpoint hit")
                break
            if reply.startswith("T") or reply.startswith("S"):
                print("stop: %s" % reply)
                hit = show(stub, int(args.wp[0], 16) if args.wp else 0,
                           "stopped")
                break
        if hit is None and not args.wp:
            print("no stop event seen")
    finally:
        sock.close()
        proc.kill()
        proc.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
