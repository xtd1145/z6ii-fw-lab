# 主板照片识读：两颗 128 MB Flash 的归属

日期：2026-11-12（19:20 修订）
输入：两张 Z6 II 主板照片（背面 / 正面）+ 用户补充的板级拓扑
**用户补充的关键事实：正面一颗、背面一颗；正面挂两颗 CPU，背面挂 STM32。**

结论修订：**物理上确实有两颗 128 MB，但被固件寻址的只有一颗半 —— 正面那颗（SoC 侧，64 MB 窗口）；背面那颗（STM32 侧）在正常启动路径里零访问。**

---

## 0. 板级拓扑（用户提供）

| 面 | 主芯片 | 128 MB Flash |
|---|---|---|
| 正面 | 两颗 CPU（EXPEED 双引擎） | 一颗，挂 SoC |
| 背面 | STM32（controller） | 一颗，挂 STM32 |

```
正面  [CPU0/CPU1 双引擎] ── 128MB NOR #1  ──→ SoC 映射在 0x10000000，只有 64MB 窗口
背面  [STM32F412ZE]      ── 128MB NOR #2  ──→ 正常 boot 零访问
```

## 1. 照片 1（背面）：读到了 controller 的真身

红框上方那颗 ST 芯片丝印清晰可读：

```
ST ARM
STM32F412
ZEJ6
```

**`ZE` = 512 KiB flash，LQFP144。**

| 证据 | 内容 | 一致性 |
|---|---|---|
| controller 固件大小 | `ex1850_012000.bi` = 524,288 B = **512 KiB** | 与 `ZE` 完全吻合 |
| 固件里的 flash 驱动 | 写 `0x40023c00` 的 CR/SR，SR 掩码 `0x1f2` | STM32F4 FLASH 接口标准语义 |
| QEMU 模型 | `Z72ControllerFlashShim` | 寄存器布局对上 |

**副作用（已验证）**：模型从 1 MiB 改成 512 KiB；扇区号按 F412 的 512 KiB 表解析：

```
snb=2 -> 0x08008000..0x0800BFFF (16 KiB)
snb=3 -> 0x0800C000..0x0800FFFF (16 KiB)
```

都在 `0x08004000..0x0800FFFF` 的数据窗口内，**不是常驻代码** —— 上次标注的"扇区布局未确证"因此关闭。

**红框里那颗 BGA** = 用户所说的背面 128 MB flash（挂 STM32）。丝印被"星星数码"水印遮住，读不出型号。

## 2. 照片 2（正面）：圈出的芯片

丝印（旋转 180° + 增强后可读）：

```
1Y2
H2A15
RB162
(e1 无铅标记)
```

**`RB162` 正是 NOR dump 文件名里的编号** —— `MT28FW01GABA1L(RB162)@BGA64`。

所以这颗 = **正面那颗、已 dump 的 128 MB 并行 NOR**。

## 3. 固件侧：只有正面那颗被寻址

### 证据 A：两个引擎映射的是同一个窗口

从两侧固件**自己的页表**（TTBR0 表 @ VA `0x10030000`，file `0x10000`）反推：

| 引擎 | NOR 窗口 | 物理地址 |
|---|---|---|
| MAS | `0x10000000..0x13FFFFFF` | PA `0x10000000..0x13F00000` |
| SLA | `0x10000000..0x13FFFFFF` | **同一个** PA `0x10000000..0x13F00000` |

**两个引擎共享同一个 64 MB 物理窗口，没有第二个 NOR 窗口。**

（顺带：`0x14000000` 以上在两侧都是 DDR 别名，PA 落在 `0xc6xxxxxx`/`0xd0xxxxxx`，不是第二个 flash。）

### 证据 B：背面那颗在正常启动里零访问

QEMU 跑 45 秒，开 `-d unimp,guest_errors`：

```
0x60000000 (FMC Bank1)     0 次
0xA0000000 (FMC 寄存器)     0 次
0xA0001000 (QUADSPI)        0 次
0x90000000 (QSPI 映射窗口)   0 次
unassigned 访问              0 次
```

**controller 从头到尾没碰过外部存储器。**

（固件里确实有这些地址，但反汇编出来是重复的 `ands r1, r0` —— 典型的 **16 位数据表项**，不是触摸这些寄存器的代码。）

### 证据 C：dump 的后 64 MB 全是 0xFF

| 块 | 范围 | 非 0xFF | 熵 | 内容 |
|---|---|---|---|---|
| 0 | `0x00000000..0x04000000` | 57,576,669（85.8%） | 4.904 | **全部内容** |
| 1 | `0x04000000..0x08000000` | **0（0.000%）** | — | **纯 0xFF** |

所有魔数（LAPP ×1、ENDM ×76、ARM 向量 ×13）全在块 0。

**正面这颗 128 MB 只用了一半。**

## 4. 背面那颗是什么状态（三种可能，按可能性排序）

| 可能 | 支持 | 反对 |
|---|---|---|
| **A. STM32 的固件仓库 / 升级暂存区**，仅在更新模式访问 | STM32 有完整 flash 擦写驱动；board 上确实有这颗芯片 | 正常 boot 零访问；若只是仓库，为何不读 |
| **B. 只被 STM32 的 16 KiB 常驻 loader 使用**，loader 在跳到主固件后就不再碰 | 常驻段 `0x08000000..0x08003FFF` 符合"小 loader"形态 | 我们的 trace 覆盖复位起，应该能看到 |
| **C. 在某个任务上下文（per-task TTBR0）里才映射** | SLA 调度器是 MMU 上下文切换式，每任务独立 TTBR0 | 任务队列一直是空的，无法验证 |

**三种都不影响现状**：QEMU 的 boot 路径不需要它。

## 5. 要拿到背面那颗，怎么做

1. **按同样方式 dump**：若也是 `MT28FW01GABA1L`（BGA64），用同一套编程器流程，读出应为 128 MB。
2. **读出来先看三件事**（脚本可直接跑）：
   - `0x0` 是否是 Cortex-M 向量表（`SP` 落在 `0x2000xxxx`、`Reset` 落在 `0x08xxxxxx`）→ 是则含 STM32 固件
   - 是否有 `0x08000000` 的镜像头 / 更新包容器魔数
   - 后 64 MB 是否也是 0xFF
3. **拿到后接进 QEMU**：给 controller 的 FMC Bank1 加一个 `-drive` 映射即可（约 30 行），模型已留好 `ctrl-flash` 观测点。

## 6. 对 QEMU 项目的影响

- **boot 不需要第二颗** —— 零访问，不加也能跑通全部现有路径
- controller flash 已按真机型号修正（512 KiB + F412 扇区表）
- 将来若确认第二颗的用途，判据明确：**FMC/QUADSPI 出现非零访问**，或**页表出现新窗口**

---

## 复现命令

```bash
# 两侧页表（NOR 窗口只有一个）
python -c "
import struct
for n,p in (('SLA',r'extracted\eg1850_sla_01700'),('MAS',r'extracted\eg1850_mas_01700')):
    w=struct.unpack_from('<4096I',open(p,'rb').read(),0x10000)
    print(n, sum(1 for x in w if (x&3)==2 and x), 'sections')
"

# controller 外部存储访问（应为 0）
cd work/qemu_build
python board_probe.py --main-screen --at 45 --out-dir run/screen/fmcprobe \
  --log "unimp,guest_errors" --trace run/fmc.log
grep -cE "60000000|a0000000|90000000" run/fmc.log
```

