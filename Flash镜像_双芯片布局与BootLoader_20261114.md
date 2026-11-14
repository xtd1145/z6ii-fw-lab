# Flash 实物读取：双芯片布局与 DSC BootLoader

日期：2026-11-14
素材：`正面…164748.BIN`、`背面…164254.BIN`、`…154914.BIN`（各 134218752 字节 = 128 MB + 1 KB）
芯片：MT28FW01GABA1L（Micron 1 Gb NOR，BGA64）

---

## 一、三个 dump 是什么

用 `dump[0x20000 : 0x20000+len(image)]` 与手头镜像按 4 KB 块比对：

| 文件 | 时间 | 数据量 | 对应镜像 | 4 KB 块一致率 |
|---|---|---|---|---|
| `正面…164748` | 16:47 | 8.56 MB（0x0–0x890000） | **`eg1850_sla_01700`** | **2157 / 2157 = 100.00%** |
| `背面…164254` | 16:42 | 37.12 MB（0x0–0x2520000） | **`eg1850_mas_01700`** | **9460 / 9461 = 99.99%** |
| `…154914`（无前缀） | 15:49 | 44.4% | 带 combo45 补丁的 MAS | 99.90%（9 块差异） |

**结论**：

- **正面芯片 = SLA（从核）固件**，与 `eg1850_sla_01700` 逐字节一致
- **背面芯片 = MAS（主核）固件**，与 `eg1850_mas_01700` 只差 1 个块
- **无前缀那份是打了 combo45 补丁的 MAS**，不是第三颗芯片

### 背面那 1 个块的差异

```
flash 0x02514000 + 0x7b8 = 0x025147b8
镜像偏移 0x24f47b8（= 镜像绝对地址 0x124f47b8）
差异 2 字节
```

正好是 combo45 diff 清单里的**第 4 处（CRC-16）**。也就是说：Flash 上是原版 CRC，而 `eg1850_mas_01700` 里那份是我们做补丁时重算过的。

### 无前缀那份的身份

它在两个位置带着补丁特征：

| flash 偏移 | 内容 | 含义 |
|---|---|---|
| `0x004c96ac` | `67f0 a8b3` | combo45 的 `b.w 0x10910e00` |
| `0x0121878e` | `5600 3400 3600` | combo45 改的字符串 |

但它与 `work/combo45/mas_combo45.bin` 仍有 9 块差异，且这些差异块**恰好比 `back` 与 `nopfx` 的差异块偏移 0x20000**。说明这份的**基线不是 `eg1850_mas_01700`**，而是另一个构建——补丁是打在另一个版本上的。

要用它做实验前，得先确认基线。目前**唯一可信的原版是背面那份**。

---

## 二、Flash 地址布局

两颗芯片都是 128 MB，布局相同：

| 偏移 | 大小 | 内容 |
|---|---|---|
| `0x00000000` – `0x0001FFFF` | 128 KB | **DSC BootLoader** |
| `0x00020000` – 尾 | 固件 | MAS：36.95 MB；SLA：8.42 MB |
| 余下 | — | 全 `0xFF`（擦除态） |

三份 dump 的 `0x0–0xFFFF` 完全相同，`0x10000–0x1FFFF` 全 `0xFF`。也就是说 loader 实际只占 **64 KB**。

---

## 三、DSC BootLoader 分析（这一层模型里没有）

### 向量表与入口

```
00000000: e59ff018  ldr pc, [pc, #24]   × 7（复位/未定义/SWI/预取中止/数据中止/保留/IRQ）
0000001c: e25ef004  subs pc, lr, #4     (FIQ)
00000020: 0000e97c                      → 入口 0x0000E97C
00000024: 0000006c  00000068  00000070  00000074  00000078  0000003c
```

入口 `0xE97C` 是标准冷启动：

```
e97c:  mov r0..r12, #0
e9b0:  ldr sp, =0x00000000        ; 实际取自 0xeb40
e9b8:  mrc p15,0,r0,c1,c0,0       ; SCTLR
e9bc:  bic r0, #1                 ; 关 MMU
e9c0:  bic r0, #4                 ; 关 D-cache
e9c4:  bic r0, #0x1000            ; 关 I-cache
e9c8:  mcr p15,0,r0,c1,c0,0
e9cc:  isb
e9d0:  mrc p15,0,r0,c0,c0,5       ; MPIDR
e9d4:  and r0, #0xf
e9d8:  cmp r0, #0
e9dc:  bne 0xeb0c                 ; 非核 0 → 从核分支
```

**loader 自己就是多核启动代码**：核 0 继续，其余核走 `0xeb0c`。这与模型里的双核设定（cpu0 主 / cpu1 从）对得上。

### 字符串（说明它驱动什么）

```
RBN_EventFlag.c   Assert!!   ExsTopDriver.c   GpioDriver.c
PcieDriver.c      XqdWrapperDriver.c   xqdal_user.c
DSC-FW not exist..        Card Initialize failed..   Card not exist..
File Search failed..      File not exist..          File Size invalid..
File Open failed..        File Information Read failed..
Program Load to 0x%08x - failed.. / - done.
*** DSC BootLoader <Ver.…  ***  [Device:…][Model:…]  ***
```

**它初始化 GPIO、PCIe、XQD 卡，然后从卡上找 `DSC-FW` 文件加载**——这是固件更新路径；找不到就走 Flash 里的固件。

### 加载目标：0x10020000

```
0x6840: 40100000  4000ffff  7f0024d4  7f000000
0x6850: 10020000  "DSC-FW not exist.."
0x6b18: "Program Load to 0x" ...
```

`0x6850` 处的 `0x10020000` 就是加载目标，紧跟着“DSC-FW 不存在”的错误串。

`0xeb40` 的数据区进一步印证：

```
eb40: 00000000
eb44: 04280000     ← 与模型 Z72_PRIVATE_BASE 相同
eb48: 0428201c     ← 与模型 capseq 寄存器区相同
eb4c: 10020000     ← 固件入口
eb50: 04282104
eb54: 04282004
eb58: 00010001
eb5c: 00010000
eb60: 55555555
eb68: 7f01fec0
eb6c: 7f01bec0
```

---

## 四、对模型的意义

**1. 「复位 PC = 0x10020000」这件事被实物证实了。**

模型里这条是假设。现在 loader 的常量表明确写着加载到 `0x10020000`，而且 loader 用的私有寄存器基址 `0x04280000` / `0x0428201c` 与模型完全一致。

**2. 模型确实跳过了 loader 这一层。**

真机链路：`BootROM → DSC BootLoader（SRAM，64 KB）→ 应用固件 0x10020000`。

模型直接从 `0x10020000` 起跑，用 `boot_store` 模拟了部分加载行为。**这个简化在“应用层行为”上是合理的，但意味着 loader 做的硬件初始化（PCIe 链路、XQD 卡、GPIO 初值）在模型里是空白的**——这也解释了为什么 XQD/卡相关路径在 QEMU 里从来没有真正跑起来。

**3. 两颗芯片的分工确定：正面 SLA、背面 MAS。**

之前 `主板照片识读_第二块Flash判定.md` 是推测，现在有实物证据。模型的双引擎（master/slave）在硬件上就是**两颗独立的 NOR**，不是一个封装里的两个区。

---

## 五、待确认

1. **无前缀那份的基线**：它带 combo45 补丁但底包不是 `eg1850_mas_01700`。需要确认它是哪次刷写留下的。
2. **loader 是否需要建模**：如果后续要跑“上电到应用固件”的完整链路，得把 64 KB loader 纳入；如果只关心应用层行为，当前从 `0x10020000` 起跑是够的。
3. `0x40100000` / `0x4000ffff` 这组常量指向什么外设，尚未查。

---

## 六、复现

```bash
# 身份比对（4 KB 块）
python - <<'EOF'
FR=r'C:\Users\BI\Downloads\正面MT28FW01GABA1L(RB162)@BGA64_20261114_164748.BIN'
BK=r'C:\Users\BI\Downloads\背面MT28FW01GABA1L(RB162)@BGA64_20261114_164254.BIN'
sla=open(r'D:\z6ii_fw_lab\fw_verified\eg1850_sla_01700','rb').read()
mas=open(r'D:\z6ii_fw_lab\fw_verified\eg1850_mas_01700','rb').read()
for name,dump,img in (('front',open(FR,'rb').read(),sla),('back',open(BK,'rb').read(),mas)):
    a=dump[0x20000:0x20000+len(img)]
    same=sum(1 for i in range(0,len(img),4096) if a[i:i+4096]==img[i:i+4096])
    print(name, same, (len(img)+4095)//4096)
EOF

# 提取 loader
python -c "d=open(r'C:\Users\BI\Downloads\正面MT28FW01GABA1L(RB162)@BGA64_20261114_164748.BIN','rb').read(0x20000); open(r'D:\z6ii_fw_lab\.local\flash\loader.bin','wb').write(d)"
arm-none-eabi-objdump -D -b binary -m arm --start-address=0xe97c --stop-address=0xe9e0 .local/flash/loader.bin
```
