"""模拟执行 eg1850_mas 的自定义 Picture Control 扫描入口 0x103bb7b4，
   直接取出台阶文件名与盘符处理行为（回答问题 1 / 问题 2）。"""
import os
import struct
import sys

from unicorn import *
from unicorn.arm_const import *


def locate_fw():
    """只读定位 eg1850_mas_01700：环境变量优先，否则向上找仓库根。"""
    env = os.environ.get("Z6II_MAS")
    if env:
        return env
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        p = os.path.join(d, "all_modules", "Z6_2_0170", "eg1850_mas_01700")
        if os.path.exists(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    raise SystemExit(
        "只读输入 eg1850_mas_01700 未找到。设环境变量 Z6II_MAS=<固件路径> 后重试。\n"
        "本脚本只读固件，不写任何磁盘目标。")


MAS = locate_fw()
# 显示口径（文件偏移 + 0x10000000）。mas 的**运行期基址是 0x10020000**，给 QEMU/gdb 下断点
# 要在此基础上 +0x20000（例如本脚本入口 0x103bb7b4 -> 运行期 0x103DB7B4）。
# 本脚本只做模块内部自洽的 PC 相对取指模拟，结论与运行期基址无关。
BASE = 0x10000000
m = open(MAS, "rb").read()
sys.setrecursionlimit(10000)

WANT = [0x103BB7B4, 0x1033D2DC, 0x1033D1C0, 0x1033D1BA, 0x104EC652, 0x104EC6BA,
        0x104B6834, 0x104B69EA, 0x103BB6DA, 0x104E9568, 0x103B9F74, 0x103D8E26,
        0x103BA296, 0x103C321E, 0x103BC77E, 0x103DC844, 0x103BA5BA, 0x103BC5EA]
CODE_PAGES = sorted({a & ~0xFFF for a in WANT})
RAM_PAGES = [0xC188B000, 0xC29AF000, 0xC29A8000, 0xC2992000]
STACK = 0x20000000

mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
for p in CODE_PAGES:
    off = p - BASE
    mu.mem_map(p, 0x1000, UC_PROT_ALL)
    mu.mem_write(p, m[off:off + 0x1000])
for p in RAM_PAGES:
    mu.mem_map(p, 0x1000, UC_PROT_ALL)
mu.mem_map(STACK - 0x10000, 0x40000, UC_PROT_ALL)

# 让 ldr r0,[r6,#0x14] 读到 0（未导入标志），[r6,#8] 读到非 0（有效对象）
mu.mem_write(0xC188B2A4 - 0xC + 0x14, struct.pack("<I", 0))
mu.mem_write(0xC188B2A4 - 0xC + 0x08, struct.pack("<I", 0xC29A8004))

LOG = []


def cstr(addr, n=64):
    b = bytes(mu.mem_read(addr, n))
    z = b.find(b"\0")
    return b[:z if z >= 0 else n].decode("latin1")


def rd32(a):
    return struct.unpack("<I", bytes(mu.mem_read(a, 4)))[0]


def fmt_apply(dst, fmt, args):
    """极简 snprintf：%s 与 %02d。"""
    out = ""
    i = 0
    ai = 0
    while i < len(fmt):
        if fmt[i] != "%":
            out += fmt[i]
            i += 1
            continue
        j = i + 1
        while j < len(fmt) and fmt[j] not in "sdc":
            j += 1
        conv = fmt[i:j + 1]
        v = args[ai] if ai < len(args) else 0
        ai += 1
        if conv.endswith("s"):
            out += cstr(v & 0xFFFFFFFF)
        elif conv == "%02d":
            out += f"{v:02d}"
        else:
            out += str(v)
        i = j + 1
    mu.mem_write(dst, out.encode("latin1") + b"\0")
    LOG.append(("snprintf", fmt, [hex(a) if a > 0x1000 else a for a in args], out))
    return len(out)


def hook_code(mu, addr, size, user):
    pc = addr
    r = mu.reg_read
    w = mu.reg_write
    if pc in (0x1033D2DC, 0x1033D1C0, 0x1033D1BA, 0x104EC652, 0x104EC6BA,
              0x104B6834, 0x104B69EA, 0x103BB6DA, 0x104E9568, 0x103B9F74,
              0x103D8E26, 0x103BA296, 0x103C321E):
        ret = 1 if pc in (0x104B69EA, 0x103BB6DA, 0x104E9568, 0x103B9F74) else 0
        if pc == 0x1033D2DC:                    # memset(dst, len, 0)
            d, ln = r(UC_ARM_REG_R0), r(UC_ARM_REG_R1)
            mu.mem_write(d, b"\0" * min(ln, 0x400))
        if pc == 0x104B69EA:
            LOG.append(("open", cstr(r(UC_ARM_REG_R0))))
        w(UC_ARM_REG_R0, ret)
        w(UC_ARM_REG_PC, r(UC_ARM_REG_LR))
        return
    if pc == 0x103DC844:                        # snprintf(dst, fmt, a1, a2, a3, a4)
        dst = r(UC_ARM_REG_R0)
        fmt = cstr(r(UC_ARM_REG_R1))
        sp = r(UC_ARM_REG_SP)
        args = [r(UC_ARM_REG_R2), r(UC_ARM_REG_R3), rd32(sp), rd32(sp + 4)]
        n = fmt_apply(dst, fmt, args)
        w(UC_ARM_REG_R0, n)
        w(UC_ARM_REG_PC, r(UC_ARM_REG_LR))
        LOG.append(("path_after_snprintf", cstr(dst)))
        return
    if pc == 0x103BC77E:                        # 盘符写入：真执行前先记录
        LOG.append(("before_drive_patch", cstr(r(UC_ARM_REG_R0))))
        # 手工执行其语义：bl 0x104e9568 的返回值决定 'A'/'B'
        buf = r(UC_ARM_REG_R0)
        ret = r(UC_ARM_REG_R0)
        return


def hook_mem(mu, access, addr, size, value, user):
    try:
        mu.mem_map(addr & ~0xFFF, 0x1000, UC_PROT_ALL)
        if BASE <= (addr & ~0xFFF) < BASE + len(m):
            mu.mem_write(addr & ~0xFFF, m[addr - BASE:addr - BASE + 0x1000])
        return True
    except Exception:
        return False


mu.hook_add(UC_HOOK_CODE, hook_code)
mu.hook_add(UC_HOOK_MEM_UNMAPPED, hook_mem)

slot = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0xC8
sp = STACK + 0x10000
mu.reg_write(UC_ARM_REG_SP, sp)
mu.reg_write(UC_ARM_REG_R0, slot)
mu.reg_write(UC_ARM_REG_LR, 0)
try:
    mu.emu_start(BASE + 0x3BB7B4 | 1, 0, count=400000)
except UcError as e:
    print("halt:", e, "pc=%08x" % mu.reg_read(UC_ARM_REG_PC))

print(f"=== slot 0x{slot:03X} (index {slot-0xC7}) ===")
for e in LOG:
    if e[0] == "snprintf":
        print(f"  fmt   = {e[1]!r}")
        labels = ["a1(dir)", "a2", "a3", "a4"]
        for lab, v in zip(labels, e[2]):
            print(f"  {lab:9s} = {v}")
        print(f"  结果  = {e[3]!r}")
    elif e[0] == "path_after_snprintf":
        print(f"  缓冲首字节 = {e[1][:1]!r}  全长 {len(e[1])}")
    elif e[0] == "before_drive_patch":
        print(f"  盘符改写前 = {e[1]!r}")
    elif e[0] == "open":
        print(f"  实际打开   = {e[1]!r}")
print("\n  0x103bc77e 语义复核（反汇编）：bl 0x104e9568 -> cbz r0 ? 'A' : 'B' -> strb [r4]，"
      "r4=传入缓冲区首地址=路径字符串第 0 字节")
