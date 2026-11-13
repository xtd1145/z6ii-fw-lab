# QEMU 局限消除 · 第 2 批

日期：2026-11-12
范围：IDMAC 结果字 · NOR 写保护/OTP · LCDC 线引擎

---

## 结论表

| # | 条目 | 结果 | 说明 |
|---|---|---|---|
| 1 | IDMAC 结果字 | ✅ **已消除** | 结果由驱动 kick 触发，模型不能凭空报成功 |
| 2 | NOR 写保护 / OTP | ⚠ **无可测对象** | QEMU 里不存在 NOR 访问路径（见 §2 证据） |
| 3 | LCDC 线引擎 | 🟡 **部分** | 帧周期可配 + 读 trace 已做；真线扫描缺固件侧证据，未改 |

---

## 1. IDMAC 结果字：从"恒报失败"到"kick 驱动"

### 原状

```c
if (address == 0x2003016c || address == 0x2005016c) {
    return; /* Result word is read-only; zero is the success assumption. */
}
```

注释写"零代表成功"，但**反汇编证明写反了**：

```asm
0x10024540: ldr sb,[pc]         ; sb = 0x20023000（控制块）
0x1002456c: add r7,sb,#0xd000   ; r7 = 0x20030000（IDMAC）
0x10024578: str r5,[sb,#0x318]  ; kick
0x1002457c: ldr r0,[r7,#0x16c]  ; ★ 读结果字
0x10024580: cmp r0,#0
0x10024584: movne r6,#1         ; ★ 非零 → r6 = 1（成功）
```

`r6` 是返回码：**0 = 失败，1 = 成功，0x4000 = 超时**。
所以非零才是成功，而模型恒返回 0 —— **每一次 DMA 都被报成失败**。

### 改法

结果字由驱动自己的 kick 写arms：

| 事件 | 效果 |
|---|---|
| 写 `0x20023118` / `0x20023318`（unit 0 kick） | `idmac_result[0] = 1` |
| 写 `0x20043118` / `0x20043318`（unit 1 kick） | `idmac_result[1] = 1` |
| 读 `0x2003016c` / `0x2005016c` | 返回对应结果 |
| 写结果字 | 忽略（只读） |

新增属性：`idmac-trace`（bool）、只读计数 `idmac-kicks` / `idmac-reads`。

**关键性质**：结果非零必须有固件先发 kick，模型不能自己宣布成功。

### 验证

`-global nikon-z72-boot-config.idmac-trace=on`，48 s：

```
Z62_IDMAC kick unit=0 addr=20023118 value=1 pc=1002454c
Z62_IDMAC kick unit=0 addr=20023318 value=1 pc=10024578
Z62_IDMAC read unit=0 value=1          pc=1002457c   ← 读到成功
...共 156 条
```

PC 与反汇编逐条吻合。HUD 正常：45 s `nonblack=1366 / uploads=274`。

---

## 2. NOR 写保护 / OTP：为什么这条消不掉（附证据）

### 证据一：QEMU 里没有 NOR 芯片模型

```
grep -rniE "m25p80|spi.*nor|nor.*spi|flash.*chip|n25q|w25q"  →  空
```

`nikon-z72-spi.c` 有 11 条 SSI 总线（`ssi_create_bus`），但**一条都没挂设备**。

### 证据二：固件的串行口不是 NOR

新增 `Z62_SPI_TRACE` 后实测：只有 **ch=6** 有流量，且全是 16 位字：

```
000f 6243 007f 0000 0008 01c4 001c 0018 0025 0000 0026 0000 0019 01c0 ...
```

这是"16 位寄存器地址 + 数据"的私有协议，不是 SPI NOR 的
"8 位命令 + 3 字节地址"格式。

### 证据三：固件镜像不经 NOR

`master-boot-image=` / `slave-boot-image=` 由 `loader` 设备直接注入 RAM；
controller 的 flash 是 `controller-flash-data=.local/z62-smoke/resident.bin`（48 KiB 文件）。

### 结论

**QEMU 里不存在"固件读/写 NOR"这条路径**，所以：

| 想测的 | 为什么测不了 |
|---|---|
| 写保护位阻止写入 | 没有 NOR 芯片，也没有写命令 |
| OTP 一次性语义 | 同上 |
| 更新流程写坏 flash | 更新流程（bootloader + 更新包）本身未建模 |

**这条属于"更底层局限的上游"**：要消除它，先要有"固件更新流程"的模型，
而那需要 bootloader 侧建模（ex1850 controller 从 flash 加载），工作量远大于一个芯片模型。

### 已建立的观测手段（留给将来）

- `Z62_SPI_TRACE=1`：记录 SPI tx 字节（上限 200）
- 实测确认：**ch=6 是唯一的 SPI 活动通道**，48 s 内 200+ 字

将来若接入 NOR 模型，这条 trace 就是判断"固件是否真的在用"的第一手依据。

---

## 3. LCDC：做了能做的，没做会破坏行为的

### 定位 V24 盲区的确切机制

```c
static uint64_t lcd_read(void *opaque, hwaddr offset, unsigned size)
{
    return Z62_LCD(opaque)->regs[offset / 4];   // ← 无任何动态值
}
```

**固件读 LCD 状态，读回的是它自己写进去的值。**
唯一的"硬件行为"是 `lcd_present()` 每 **60 ms** 置一次 `0x5214 bit16`（帧完成）。

注意寄存器映射：`lcd_present` 里 `r = regs + 0x4000/4`，所以
`r[0x1200/4]` 实际是 **off 0x5200**、`r[0x1214/4]` 是 **off 0x5214** ——
trace 里固件正好在读这两个（11 次 / 4 次），对得上。

### 已做

| 改动 | 价值 |
|---|---|
| `frame-ms` 属性（默认 60，钳制 ≥1） | 帧率相关 bug 现在可测；原来硬编码 60 ms，任何与刷新周期耦合的节奏问题在模型里完全隐形 |
| `Z62_LCD_TRACE=1` | 记录固件读的 LCD 寄存器地址（实测：0x65c0 ×21、0x5510 ×12、0x5200 ×11、0x5214 ×4 …） |

验证：`-global nikon-z62-lcd.frame-ms=30` → HUD 正常（nonblack=1366）。

### 没做真线扫描，原因

要做到"逐行扫描"（让帧中途改 VRAM 地址产生可见撕裂），必须改 `lcd_present` 的
整帧合成路径，分多个 timer tick 推进。**风险在于把它从"原子一帧"改成"多段"，
会改变帧完成时机（0x5214 bit16 的置位点），可能让现有显示路径全部走样。**

而**收益无法验证**：trace 显示固件只在读 0x5200/0x5214/0x65c0 等状态寄存器，
**没有找到任何"行计数 / 扫描位置"的读取证据**。在不知道固件依赖哪个信号的情况下
把它做成行计数，等于凭空发明一个硬件行为 —— 那正是这份清单要避免的错误。

**所以停在"可配帧周期 + 可观测"这一层，并如实标注为部分完成。**

### 如果将来要补真线扫描，前置条件是

1. 找到固件里"等某一行/某个扫描位置"的代码（而不是只等帧完成）；
2. 或拿到 V24 黑屏包在真机上的失败时刻（哪一帧、哪个区域），反推它期望的时序信号。

---

## 4. 三条汇总

| 条目 | 状态 | 可验证性 |
|---|---|---|
| IDMAC 结果字 | ✅ 消除 | `idmac-trace` 日志 + HUD 回归 |
| NOR 写保护/OTP | ⚠ 无可测对象 | 三条 grep/实测证据；观测手段已留 |
| LCDC 线引擎 | 🟡 部分 | `frame-ms` 可配已验；线扫描缺前置证据 |

## 5. 复现命令

```bash
# IDMAC 结果字
python work/qemu_build/board_probe.py --main-screen --at 45 \
  --out-dir run/screen/idmac --extra "nikon-z72-boot-config.idmac-trace=on"

# SPI 观测（确认固件碰不碰 NOR）
Z62_SPI_TRACE=1 python work/qemu_build/board_probe.py --main-screen --at 48

# LCD 读观测 + 帧周期
Z62_LCD_TRACE=1 python work/qemu_build/board_probe.py --main-screen --at 48 \
  --extra "nikon-z62-lcd.frame-ms=30"
```
