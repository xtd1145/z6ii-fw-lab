#!/usr/bin/env python3
"""核心负载账（只读，不写任何固件）

回答两个问题：
  1) sla 到底是不是闲着的？用什么指标证明？
  2) 若要把 mas 的活挪给 sla，硬件主权边界在哪、IPC 面在哪？
"""
import struct, sys
from collections import Counter, defaultdict

M = "extracted/eg1850_mas_01700"
S = "extracted/eg1850_sla_01700"
MAS_BASE = 0x10000000


def words(b):
    return memoryview(b).cast("I")


def vma_base(img, probe=1 << 20):
    """取镜像里最像自身代码段的地址高位前缀"""
    w = words(img[:probe])
    c = Counter(x >> 16 for x in w if x >> 16 >= 0x1000 and x >> 24 < 0x20)
    top, n = c.most_common(1)[0]
    return top << 16, c.most_common(6)


def region(a):
    if 0x10000000 <= a < 0x14000000:
        return "code/ram-high"
    if 0x40000000 <= a < 0x50000000:
        return "MMIO"
    if 0xC0000000 <= a < 0xD0000000:
        return "DRAM-shared"
    if 0x20000000 <= a < 0x30000000:
        return "SRAM"
    if 0x80000000 <= a < 0xA0000000:
        return "flash-map"
    return None


def refs(img, base, size):
    """镜像内所有 u32 常量 -> 目标地址，按 64KB 块聚合。返回 (block->count, addr->count)"""
    blk, adr = Counter(), Counter()
    w = words(img)
    for x in w:
        r = region(x)
        if r is None:
            continue
        if base <= x < base + size:          # 指向自己镜像内部，不算外设主权
            continue
        blk[(r, x >> 16)] += 1
        adr[(r, x)] += 1
    return blk, adr


PROLOG = (b"\x2d\xe9", b"\x36\xb0", b"\xb4\xb0", b"\xb5\xb0", b"\xb0\xb4", b"\x2d\xe8")


def code_density(img):
    """每 64KB 块的函数序言命中数 -> 代码段/数据段地图"""
    out = []
    for off in range(0, len(img), 1 << 16):
        ch = img[off:off + (1 << 16)]
        h = sum(ch.count(p) for p in PROLOG)
        out.append((off, h))
    return out


def main():
    mas = open(M, "rb").read()
    sla = open(S, "rb").read()

    mb, mt = vma_base(mas)
    sb, st = vma_base(sla)
    print(f"mas len={len(mas):,}  VMA base~0x{mb:08X}  end=0x{MAS_BASE+len(mas):08X}")
    print(f"sla len={len(sla):,}  VMA base~0x{sb:08X}  end=0x{sb+len(sla):08X}")
    print(f"  mas top prefixes: " + " ".join(f"0x{t:04X}:{n}" for t, n in mt))
    print(f"  sla top prefixes: " + " ".join(f"0x{t:04X}:{n}" for t, n in st))
    print(f"  相邻性: mas_end=0x{MAS_BASE+len(mas):08X}  sla_base=0x{sb:08X}  间隙={sb-(MAS_BASE+len(mas)):+,}")

    mb_blk, mb_adr = refs(mas, MAS_BASE, len(mas))
    sb_blk, sb_adr = refs(sla, sb, len(sla))

    print("\n=== 1. 每核硬件主权（引用最多的 64KB 外设块）===")
    for name, blk in (("mas", mb_blk), ("sla", sb_blk)):
        print(f"-- {name} --")
        for (r, b), n in blk.most_common(12):
            print(f"   {r:<12} 0x{b:08X}0000  x{n}")
        tot = defaultdict(int)
        for (r, _), n in blk.items():
            tot[r] += n
        print("   合计: " + "  ".join(f"{k}={v}" for k, v in sorted(tot.items())))

    shared_blocks = set(mb_blk) & set(sb_blk)
    print(f"\n=== 2. 两核共同引用的外设块：{len(shared_blocks)} 个（握手/共享 DMA 通道候选）===")
    for k in sorted(shared_blocks, key=lambda k: -(mb_blk[k] + sb_blk[k]))[:18]:
        r, b = k
        print(f"   {r:<12} 0x{b:08X}0000   mas x{mb_blk[k]:<6} sla x{sb_blk[k]}")

    both = set(mb_adr) & set(sb_adr)
    print(f"\n=== 3. 精确地址交集：{len(both)} 个（真 IPC 描述符）===")
    for k in sorted(both, key=lambda k: -(mb_adr[k] + sb_adr[k]))[:20]:
        r, a = k
        print(f"   {r:<12} 0x{a:08X}   mas x{mb_adr[k]:<6} sla x{sb_adr[k]}")

    print("\n=== 4. 忙等/休眠指令（判断 sla 是否饥饿）===")
    for name, img in (("mas", mas), ("sla", sla)):
        wfi = img.count(b"\x10\x0a\x43\xee") + img.count(b"\x30\x0a\x43\xee")
        wfe = img.count(b"\x10\x06\x43\xee")
        twfi = img.count(b"\x30\xbf")
        evt = img.count(b"\x12\x06\x43\xee")          # WFE (event) -> IPC doorbell
        print(f"   {name}: ARM wfi={wfi} wfe={wfe} evt={evt} | Thumb wfi-ish(BF30)={twfi}  密度=1/{max(1,len(img)//max(1,wfi+wfe+1))}")

    print("\n=== 5. 代码段地图（每 1MB 的函数序言密度）===")
    dm, ds = code_density(mas), code_density(sla)
    def agg(d, per=1 << 20):
        out = defaultdict(int)
        for off, h in d:
            out[off // per] += h
        return out
    am, as_ = agg(dm), agg(ds)
    mcode = sum(1 for _, h in dm if h > 40)
    scode = sum(1 for _, h in ds if h > 40)
    print(f"   mas: 64KB 块含代码 {mcode}/{len(dm)}  ≈{mcode*64:,} KB 代码")
    print(f"   sla: 64KB 块含代码 {scode}/{len(ds)}  ≈{scode*64:,} KB 代码")
    print("   mas 1MB 段: " + " ".join(f"{k}:{v}" for k, v in sorted(am.items()) if v))
    print("   sla 1MB 段: " + " ".join(f"{k}:{v}" for k, v in sorted(as_.items()) if v))

    print("\n=== 6. 跨核调用方向（按目标落在对方镜像内统计）===")
    def cross(img, obase, osize, base, size, name):
        n = Counter()
        for x in words(img):
            if obase <= x < obase + osize:
                loc = x - obase
                n[(loc >> 20) & ~1] += 1
        return n
    # mas 里指向 sla 镜像范围
    cm = cross(mas, sb, len(sla), MAS_BASE, len(mas), "mas->sla")
    cs = cross(sla, MAS_BASE, len(mas), sb, len(sla), "sla->mas")
    nm, ns = sum(cm.values()), sum(cs.values())
    print(f"   mas 中的常量指向 sla 镜像: {nm:,}   分布(每1MB): " + " ".join(f"{k}:{v}" for k, v in sorted(cm.items())[:12]))
    print(f"   sla 中的常量指向 mas 镜像: {ns:,}   分布(每1MB): " + " ".join(f"{k}:{v}" for k, v in sorted(cs.items())[:12]))
    if nm:
        print(f"   比值 sla->mas / mas->sla = {ns/nm:.1f}x")


if __name__ == "__main__":
    sys.exit(main())
