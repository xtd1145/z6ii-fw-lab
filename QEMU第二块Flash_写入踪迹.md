# 第二块 Flash：写入踪迹确认 + 模型补齐

日期：2026-11-12
结论：**找到了，而且不是"没有路径"——真参数下 controller 每轮 boot 都会解锁并擦除它的 flash。**

---

## 1. 第二块 Flash 是什么

不是 SPI NOR，是 **controller（Cortex-M4，`ex1850_012000`）自己的片上 flash**：

| 项 | 值 | 来源 |
|---|---|---|
| 本体 | 1 MiB @ `0x08000000` | `nikon-z72-controller.c:511` `memory_region_init_rom(..., 0x100000)` |
| 前 48 KiB | controller 固件（resident） | `z72-controller-resident-flash`，要求严格 49152 B |
| 控制接口 | `0x40023c00`，0x100 字节 | STM32 风格 FLASH 寄存器组 |
| 复位别名 | 同内容映射到 `0x0` | `z72-controller-reset-alias` |

原注释直接写着：*"Flash operations and the controller-to-engine link are absent."*
—— 也就是说，这条路径以前**完全没有模型**。

## 2. 固件侧确有完整擦写驱动

controller 固件未加密（熵 3.687，标准 M4 向量表 `08 0c 00 20 / b5 02 00 08`）。反汇编确认：

```
0x08001f10: bic r1, r1, #0x400      ; 控制位清/置序列
0x08001f18: orr r1, r1, #0x1000
0x08001f28: orr r1, r1, #0x400
0x08001f92: str r2, [r1]            ; 写 CR
0x08001fa4: str r0, [r1]  ; r0=2    ; ★ CR = 2 → SER（扇区擦除）
0x0800204a: ands r0, r0, #0x1f2     ; ★ SR 错误掩码 OPERR/WRPERR/PGAERR/PGPERR/PGSERR
0x0800204e: beq  ...                ; 无错误 → 成功分支
```

`0x1f2` 这个掩码是 STM32 FLASH_SR 的标准错误位组合，与 `0x40023c00` 的寄存器布局互相印证。

## 3. 实际运行踪迹（真参数，55 s）

新增 `Z62_CTRLFLASH_TRACE=1` 观测后抓到：

```
wr off=004 = 45670123        ← FLASH_KEY1
wr off=004 = cdef89ab        ← FLASH_KEY2（解锁完成）
wr off=010 = 00000212        ← SER + SNB=2 + PSIZE
wr off=010 = 00010212        ← 加 STRT 启动擦除
rd off=00c -> 00000000  ×3   ← 轮询 SR 的 BSY
wr off=010 = 00010200        ← 清 SER
... ACR 位序列 ...
wr off=010 = 80010201        ← PG + STRT（页编程）
wr off=010 = 00000212 / 00010212  ← 第二轮：SNB=3
```

统计：**1 次解锁、2 次扇区擦除（SNB=2、SNB=3）、97+ 次页编程**。

### 关键：这条路径由参数触发

| 参数 | CTRLFLASH 事件 | 解锁 | 擦除 |
|---|---|---|---|
| 实验室参数 | 20 条（仅 ACR 读写） | 0 | 0 |
| **真 NOR 参数** | 800+ 条 | **1** | **2** |

真参数让固件走进"参数迁移/落盘"路径，实验室参数不走。这也解释了为什么此前所有实验都没发现这条路径。

## 4. 旧模型的问题：替硬件判成功

`0x40023c00` 原本是 `memory_region_init_ram` —— 纯被动存储：

- 固件写 `CR=0x10212` 启动擦除 → 被存下来，**无任何副作用**
- 固件读 `SR` → 返回它自己上次写的值（**0**）→ 立刻判定"BSY 清零、擦除成功"
- 固件写 `0x0800xxxx` → ROM 静默丢弃

所以任何"擦除失败 / 写保护 / 校验不过"的 bug 在这套模型下**不可能出现**。这与 v9 的 gate 恒 ack 是同一型。

## 5. 补上的模型语义

`nikon-z72-controller.c` 新增 `Z72ControllerFlashShim`：

| 寄存器 | 建模 |
|---|---|
| `KEYR/OPTKEYR` (0x04/0x08) | 两键解锁序列，错误顺序重置 |
| `CR` (0x10) | `LOCK` 反映解锁状态；`STRT` + `SER/PG/MER` 启动命令；未解锁时写入被忽略 |
| `SR` (0x0c) | `BSY`(bit16) 在命令期间置位、`EOP`(bit0) 完成置位、失败置 `WRPERR`(bit4)、写 1 清除 |
| 命令时长 | 擦除 20 ms、编程 100 µs（实验室标定值，非实测） |

失败注入：`Z62_CTRLFLASH_FAIL=1` → 每次擦除/编程都报 `WRPERR`。

## 6. 失败注入的实际效果：复现黑屏

| 模式 | 50 s | 55 s | 60 s | 70 s |
|---|---|---|---|---|
| 擦除成功 | 1296 | 1296 | 1296 | 1296 |
| **擦除失败** | **0（黑屏）** | **0（黑屏）** | 1296（恢复） | 1296 |

失败时 CPU 状态也变了：

```
50s  cpu0=0x10242fd6（代码区）  cpu2=0x10038a68（idle）
55s  cpu0=0x1023c226（0x80 元素批量拷贝循环内）  cpu2=0x10038a64
60s  cpu0=idle，画面恢复
```

固件重试 6 次后恢复。**这条"擦除失败 → 黑屏 → 重试 → 恢复"的完整行为，在旧模型下不可能出现。**

## 7. 诚实边界

1. ~~**扇区布局未确证**~~ → **已确证（2026-11-12 补）**。主板照片显示 controller 是 **STM32F412ZE**（LQFP144，`ZE` = 512 KiB flash）。按 F412 的 512 KiB 扇区表（4×16K + 1×64K + 3×128K）解析：

   ```
   SER/MER snb=2 -> 0x08008000..0x0800BFFF (16 KiB)
   SER/MER snb=3 -> 0x0800C000..0x0800FFFF (16 KiB)
   ```

   两个扇区都落在 `0x08004000..0x0800FFFF` 的 **flash-data 数据窗口**内，**不是常驻代码所在的 sector 0（0x08000000..0x08003FFF）**。所以"固件擦自己正在运行的代码"这个矛盾不存在了。
   模型已从 1 MiB 改成 512 KiB（`memory_region_init_rom(..., 0x80000)`），越界扇区号不再被超额映射吸收。
2. **擦除/编程未真正改内容**。flash 本体仍是 ROM，写入被丢弃。要让"擦除后读到 0xFF"成立，需要把本体改成可写并实现擦除语义。
3. **命令时长为实验室标定**（20 ms/100 µs），不是实测值。
4. `Z62_CTRLFLASH_FAIL` 是**故意失败注入**，不是"真机会失败"。它只证明"这条分支现在可测"，不证明真机行为。

## 8. 复现命令

```bash
cd D:/z6ii_fw_lab/work/qemu_build

# 观测踪迹（真参数）
Z62_CTRLFLASH_TRACE=1 python board_probe.py --main-screen --at 55 \
  --out-dir run/screen/flash_real \
  --aux ".local/parameters_real/group17.bin@0x132c0000,,.local/parameters_real/group18.bin@0x132e0000,,.local/parameters_real/group32.bin@0x13e80000" \
  --extra "nikon-z62-lcd.live-view-fill=on"

# 注入擦除失败（应看到黑屏后恢复）
Z62_CTRLFLASH_TRACE=1 Z62_CTRLFLASH_FAIL=1 python board_probe.py --main-screen \
  --at 50,55,60,70 --pc --out-dir run/screen/flash_fail \
  --aux ".local/parameters_real/group17.bin@0x132c0000,,.local/parameters_real/group18.bin@0x132e0000,,.local/parameters_real/group32.bin@0x13e80000" \
  --extra "nikon-z62-lcd.live-view-fill=on"
```
