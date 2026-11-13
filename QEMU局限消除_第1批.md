# QEMU 局限消除 · 第 1 批

日期：2026-11-12
状态：**两条局限已实质消除，均有可重复的自动化回归**

---

## 1. SLA 释放握手：从"恒 ack"到"可复现 v9 黑砖"

### 问题定位

固件 `mas 0x10200e57` 的 release 助手：

```asm
0x10200e5b: movs r0,#0xa1
0x10200e5f: str  r0,[r1,#0x10]   ; 写 0x04287010 = 0xA1（release ack）
0x10200e61: ldr  r0,[r1,#4]      ; 读 0x04287004
0x10200e63: lsls r0,r0,#0x1f     ; 取 bit0
0x10200e65: beq  0x10200e61      ; ★ bit0==0 → 死循环 = v9 的 0x10200E60
0x10200e69: str  r0,[r1,#4]      ; bit0==1 → 清 gate
```

模型原本：

```c
if (addr == 0x04287004 || addr == 0x20010038) value |= 1;   // 恒真
```

**只要读就返回"已应答"，与固件写了什么、机身是否真的应答完全无关。**

### 改法

`0x04287004` bit0 改为读真实 latch：

| 事件 | 效果 |
|---|---|
| 写 `0x04287010 = 0xA1` | `gate_ack_latched = true`（计 `gate-ack-ok`） |
| 写其它值 | latch 不置，计 `gate-ack-bad`，打 `Z62_GATE ack-mismatch` |
| `gate-ack-starve=on` | 值对也不置（模拟机身不应答） |
| 写 `0x04287004` bit0 | 清 latch（固件消费掉 ack） |
| 从未写过 release 端口 | 保持旧行为（不误伤无关路径） |

新增属性：
- `gate-ack-strict`（默认 **on**）
- `gate-ack-starve`（默认 off，用于复现 v9）
- 只读诊断：`gate-ack-ok` / `gate-ack-bad` / `gate-ack-last-bad`

### 验证

`gate_regression.py` 两态对比：

```
严格模式：cpu0=0x1065600a（真实代码）  uploads=122  未停轮询   PASS
starve  ：cpu0=0x10200e60（v9 死点）  uploads=0    停在轮询   PASS
结果: PASS
```

**这是 v9 从来没有的测试**：模型现在能区分"机身应答了"和"机身沉默"，
后者会在 QEMU 里精确停在 `0x10200e60` 并黑屏（uploads=0）。

### 一个自己踩的坑（值得记）

第一版把 `gated`（`gate_table[8] & 1`，控制是否更新 status/IRQ）
和"ack 是否成立"混成了一个条件，结果所有 `gate_table[8]` bit0 为 0 的路径
全部被卡死 —— 连官方 MAIN 都 boot 不起来。

**是"官方 MAIN 对照组"抓到的**，不是 combo45。教训：
改共享握手语义时，必须同时跑"未打补丁的原厂镜像"作对照。

---

## 2. PTP 准入分支：combo45 在 QEMU 里 6/6 验证通过

### 输入包

`combo45.signed.BIN`（49,002,888 B，sha256 `a6701df2…`，与 README 一致）：
只给 `0xF032`（读内存）在正常态开例外，`0xF033`（写）仍拒绝。

### 验证链

| 步骤 | 结果 |
|---|---|
| 包 sha256 / 尺寸 | ✅ 与 README 一致 |
| 三处 edit 的旧值（官方 MAIN） | ✅ 逐字节匹配 spec |
| CRC-16/XMODEM 校验 | ✅ 官方 stored=`8e61` = calc |
| 打补丁 + 重算模块 CRC | `8e61 → 025a`，38,750,138 B |
| 反汇编改后代码 | ✅ 与 README 伪代码逐条吻合 |
| QEMU boot | ✅ 与官方 MAIN 完全一致（uploads 280→492，nonblack 1364→1366） |
| 洞内分支单元测试 | ✅ 6/6 PASS |

### 洞代码（实测反汇编）

```asm
0x10930e00: cbnz r0, allow        ; 会话已开
0x10930e02: movw r1,#0xf032
0x10930e08: beq  allow            ; ★ 唯一新增：读内存
0x10930e0a: movw r1,#0xf001
0x10930e10: beq  allow            ; F001 原特例
0x10930e12: movw r4,#0xa004
0x10930e16: b.w  deny             ; 其余 → Access Denied
0x10930e1a: b.w  allow
```

**F033 确认不在放行列表内。**

### 6/6 测试结果

```
F032 读内存（应放行）        allow  PASS
F033 写内存（应拒绝）        deny   PASS
F001 开会话（应放行）        allow  PASS
F002 任意其它（应拒绝）      deny   PASS
F032 且会话已开（应放行）    allow  PASS
F033 且会话已开（应放行）    allow  PASS
```

### 怎么做到的（不需要 USB 栈）

洞是 `(r0, sb)` 的纯函数——无内存访问、无回调。所以用 GDB 把 PC 直接指向洞：

- `ptp_gate_probe.py`：暂停在 HUD 后，`G` 写寄存器（PC=cave|1、sb=r9、r0=模拟回调返回值），
  单步 8 条，看落在 `0x10930e1a`(allow) 还是 `0x104c96c0`(deny)，然后恢复原寄存器。

三个必须记住的 GDB 细节：

| 坑 | 现象 | 正解 |
|---|---|---|
| `P` + blob | 写寄存器被静默忽略 | 用 `G` + hex blob |
| CPSR T 位没置 | 单步跳到 `0x10100004`（Undefined 向量） | CPSR 置 `0x20`，同时 `0xC0` 关中断 |
| `sb` 当成 r11 | `cmp sb,r1` 永不相等，全判 deny | **`sb` = r9** |

### 一个附带发现

`0x104c96b2` 的 `cmp sb,r1` 在打补丁后成了**死代码**（`movw r1` 被 6 字节跳板覆盖，
且顺序执行路径已被 `b.w` 截断）。这正是 z62tool 报的
"跳板原 6 B 未逐字出现在洞里" WARN 的来源 —— 无害，不需要修。

---

## 3. 剩余条目（下一步）

| 条目 | 状态 |
|---|---|
| IDMAC 结果字 `0x2003016c` "zero is the success assumption" | 待改（任务 15） |
| NOR 写保护 / OTP | 待做（任务 16） |
| 第二颗 Flash | 待做 |
| LCDC 线引擎（替代 60ms 合成） | 待做 |
| PMU / boot ROM / 电源时序 | 待做 |
| USB PHY / 背光 | 待做 |
| CVP/FD 加速器 | 待做 |

## 4. 复现命令

```bash
# 释放握手回归（两态，约 90 s）
python work/qemu_build/gate_regression.py --at 32

# PTP 洞分支（真实固件，6 例）
python work/qemu_build/ptp_gate_probe.py --at 30

# combo45 在 QEMU 里 boot
python work/qemu_build/board_probe.py --main-screen \
  --master work/combo45/mas_combo45.bin --at 45
```
