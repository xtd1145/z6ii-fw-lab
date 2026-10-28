# 尼康 Z6 II QEMU —— 学习 xiaoqingfeng0526/nikon-z62-qemu 后的整合方案

> 整理日期：2026-10-26
> 目标仓库：https://github.com/xiaoqingfeng0526/nikon-z62-qemu（本地副本 `D:/z6ii_fw_lab/nikon-z62-qemu/nikon-z62-qemu-main/`）
> 本方案回答三件事：**这个仓库是什么、它和我们自研的 Z62 机器是什么关系、下一步怎么整合。**

---

## 一、结论速览（先说重点）

1. **仓库名字叫 nikon-z62-qemu，但公开源码主体是 Z7 II（Z72）**。`publication-manifest.json` 标注 `"model": "Nikon Z7 II"`，机器类型只有 `nikon-z72-lab` / `nikon-z72-slave-lab` / `nikon-z72-dual-lab`。README 里说的 Z62 板型 `nikon-z62-dual-lab` 与「本地 R3 运行包」**未公开**，只保留了 Z62↔Z72 的对比研究笔记。

2. **它和我们是同一个平台、同一代固件**：都是 EXPEED 6、C 1.70、五成员更新包（EX/TPJ/master/VR/slave）。Z6 II 用 **EG1850**（`eg1850_mas_01700` / `eg1850_sla_01700` / `ex1850_012000`），Z7 II 用 **EG1840**。**我们的固件就是 Z62**：`eg1850_mas_01700` 38,750,136 字节（仓库记 38,750,138，差 2 字节是 Nikon 尾部 CRC-16/XMODEM 残留）。

3. **这个仓库最有价值的是「方法论 + 已验证的设备建模模式 + 21 阶段推进地图」**，而不是可直接复制的代码（SoC/固件/内存布局/CBAR 都不同）。它已经跑到「原厂菜单 + 实验主屏 OSD」，我们还在 boot 完成后的「RTOS 空闲自旋」阶段，它的路径图就是我们下一步的路标。

4. **关键认知修正点**：控制器 `ex1850_012000` 是 **Cortex-M4 独立地址空间**（我们完全没碰）；双引擎 mas+sla 是**两套独立地址空间 + 共享通信 RAM**，不是 SMP 4 核（我们目前只跑了 mas）。

---

## 二、Z6 II vs Z7 II 精确差异（整合的前提）

来源：`research/z7ii/z6ii_update_comparison.md`（作者对两款 C1.70 整包的逐字节对照）+ 我们自研的固件证据。

| 项目 | Z6 II（本项目） | Z7 II（该仓库） |
| --- | --- | --- |
| SoC / 型号标识 | **1850** (EG1850) | 1840 (EG1840) |
| 机型串 | `Z_6_2` | `Z_7_2` |
| 原厂整包 | `Z6_2_0170.bin`（49,002,888 B） | `Z7_2_0170.bin`（48,494,024 B） |
| master | `eg1850_mas_01700` 38,750,138 B | `eg1840_mas_01700` 38,239,098 B |
| slave | `eg1850_sla_01700` 8,832,194 B | `eg1840_sla_01700` 8,834,370 B |
| 控制器 | `ex1850_012000` | `ex1840_013000`（524,290 B，Cortex-M4） |
| 整包检查入口（master 偏移） | `0x37EA40` | `0x37C638` |
| 摘要检查 | `0x392286` | `0x38FE82` |
| 字节变换 | `0x39EA3C` | `0x39C310` |
| CBAR | **0x04008000**（我们反推，GICD=+0x1000） | 0x04000000（仓库实验选择） |
| 固件 VMA base | 0x10020000 | 0x10020000（一致） |

**结论**：两款同源但字节不同。任何「直接照搬 Z72 代码到 Z62」都会因固件差异跑不起来。**能直接复用的只有方法、设备建模模式、以及验证过「同平台通用」的寄存器语义**（见下）。

---

## 三、我们 vs 仓库的进度对照（映射到 21 阶段）

仓库 `docs/porting.md` 记录了从 reset 到菜单的完整推进路径。把我们自研的进度对上去：

| 阶段（仓库） | 仓库证据 | 我们状态 |
| --- | --- | --- |
| 1 reset prefix | 页表/栈初始化，首次 MMIO `0x04264000` | ✅ 已跑通 |
| 2 early config | 4 个配置字 `0x04264000/0x420/0x04281028/0x2C` | ✅ 已建模 clk/pll |
| 3 system config | 58 stores / 57 words | ✅ 已建模 |
| 4 completion | 同步 completion + ack 语义 | ⚠️ 我们用 regfile 读回，未做 ack |
| 5 platform init | 360 stores / 48 polls | ✅（含 5 通道块 0x04282000） |
| 6 boot continuation | 24 stores + SVC 栈恢复 | ✅ |
| 7 boot memory | 有界零填充 + 完成/错误等待 | ✅ 描述符 RAM 0x24074000 |
| 8 runtime bootstrap | 页表安装 + RTOS 配置 | ✅ |
| 9 runtime services | GIC 配置 + 堆初始化 | ✅ **GIC 已建模**（我们 10-26 刚修） |
| 10 counter | 0x04266000 计数器 | ⚠️ 我们是读回桩，仓库有精确 64 位模型 |
| 11 SMP startup | 第二核启动 + 就绪握手 | ✅ 双核已跑 |
| 12 console | UART 0x2C090000 混合宽度 | ✅ 已建模 |
| 13 pin-group | 0x04280030 + 60 bank 字 0x04282000 | ✅ |
| 14 controller chain | 0x04281000/0x04280000/0x04287000 | ✅ |
| 15 snapshot | 0x20010044 门 + 19 指针 | ⚠️ 部分 |
| 16 parameter | 211 字 + 0x24074000 镜像 | ✅ |
| 17 kernel handoff | svc #6 分发 | ✅（我们已到空闲） |
| 18 scheduler idle | 双核停 0x1010705C..B0 | ✅ **我们正在这**（PC 0x10107094） |
| **19 timer tick** | **0x2B000000 定时器 + IRQ 237 → 调度器真正跑起来** | ❌ **下一步** |
| 20 dual-engine | 4 核双引擎 + 共享通信 RAM | ❌ 未做（只跑 mas） |
| 21 menu/graphics | GPU 上传窗 0x20180000 + LCD | ❌ 未做 |

**核心结论**：我们自研已经覆盖到阶段 18，卡在「RTOS 空闲自旋」——这正是仓库阶段 19（timer tick）要解决的。**下一个关键动作就是把定时器 tick 建出来，让 Z62 的调度器真正进入任务调度。**

---

## 四、可整合资产清单（按优先级）

### P0 —— 立即有用（下一步就直接照做）

1. **定时器 tick 设备模式**（`qemu/nikon-z72-timer.c`）
   - Z72 地址 `0x2B000000`（Z62 需从固件反推，可能不同）
   - 语义：`+0` channel-start、`+4` period、`+8` event mask（观测 0x300）、`+0x10` status
   - 周期性把 armed 位 latch 进 event/status → 拉高 IRQ 237 → 固件写回清零
   - `tick-us` 默认 1000（1ms）
   - **这是让 RTOS 调度器从 idle 进入真实任务的关键，我们下一步直接移植。**

2. **21 阶段推进地图**（`docs/porting.md`）——作为我们项目的主路线图。

3. **Z62↔Z72 差异表**（`research/z7ii/z6ii_update_comparison.md`）——印证我们固件就是 Z62，并给出精确偏移差异，后续跨机型对齐代码时直接查。

### P1 —— 升级我们已有但简陋的模型

4. **counter 精确 64 位模型**（`qemu/nikon-z72-counter.c`）：`+0` control / `+8` count 低 32 / `+12` count 高 32 / `+16` frequency，用 `gt_cntfrq_period_ns` 精确共享 CPU virtual clock，支持 freeze。我们的 ctr 目前是读回桩，可升级。

5. **boot-config 稀疏寄存器 + completion/ack 语义**（`qemu/nikon-z72.c` 的 `NikonZ72BootConfig`）：用「稀疏容器只暴露配置字、空洞未分配」+「写命令同步发布完成位 bit0、写 1 清零 ack」替代我们的全量 regfile。这对「就绪位轮询型外设」更贴合真实硬件语义。

6. **GIC + 定时器 PPI 细节**：仓库用 GICv2 revision=1、num-irq=256、has-security-extensions、timer PPI 29/30/27/26 接到每核 private bank。我们是 revision 默认 + num-irq=96。**num-irq 需从 Z62 固件核实**（256 vs 96）。

### P2 —— 打开更深处（更大工程，先规划后动手）

7. **双引擎架构**（`nikon_z72_dual_init`）：4 核（2 引擎×2 核），每引擎独立 address-space view，1 MiB 通信 RAM 通过 alias 在双视图映射。让 slave 真实响应 master 的 handoff，替代 stand-in 写。

8. **控制器 Cortex-M4 模型**（`qemu/nikon-z72-controller*.c`）：`ex1850_012000` 是 M4 核独立地址空间，含 adc/battery/dma/gpio/i2c/rng/rtc/spi/supply/timer。Z62 的控制器与 Z72 同系列，模型可参考。

9. **GPU/图形上传窗**（`qemu/nikon-z72-graphics.c` + `nikon-gpu.h`）：0x20180000，接 GPU 命令执行器，是菜单显示的必经之路。

10. **浏览器交互控制台**（`tools/web_console.py` + `web/`）：方向键/MENU/EXIT、重启、SD 卡挂载、`http://127.0.0.1:8662` 浏览器画面。我们目前只有 monitor/gdb 探针，可引入。

---

## 五、具体整合行动计划（建议顺序）

1. **反推 Z62 定时器地址与 IRQ 线**（阶段 19 前置）
   - Z72 是 `0x2B000000` + IRQ 237，Z62 可能不同。
   - 方法：GDB access watchpoint 抓「固件写 channel mask 0x300 的 MMIO 地址」；或扫固件找定时器基址字面量；对照 `eg1850_mas_01700` 里 event-word 0x300 的写入点。

2. **移植 timer tick 设备到 nikon_z6ii.c**，验证调度器从 `0x10107094` 空闲进入真实任务调度（观察 per-core 任务表从 0 变非零、中断计数增长）。

3. **升级 counter 为精确 64 位模型**（对照 `nikon-z72-counter.c`），并核实 Z62 的 GIC num-irq（96 or 256）。

4. **评估双引擎 + 控制器 M4**：这是「让 slave 真实响应、进入拍照路径」的前置，属大工程，单独规划。

5. **引入浏览器交互控制台**：等调度器跑起来、能进菜单后再接。

---

## 六、诚实的技术边界

- 该仓库公开的是 **Z72** 源码，Z62 的 board/display/sensor 模型在未公开的本地 R3 包。我们无法直接拿到 Z62 的实现，只能**用 Z72 的方法反推 Z62**。
- 仓库也明确标注：CPU 核型（A7）、GIC base/rev、timer 周期等都是「实验选择」，不是恢复出的硬件事实。**我们的 A15 选择有 `udiv` 指令的实证支撑，比仓库的「实验选择」更有依据**，不必照搬 A7。
- 仓库当前也「尚不能正常取景、拍照、录像」——它跑通的是菜单和实验主屏，距离我们最终目标（拍照路径）仍有距离。整合它是「借地图加速」，不是「直接到终点」。

---

## 附：仓库本地副本位置

- 源码：`D:/z6ii_fw_lab/nikon-z62-qemu/nikon-z62-qemu-main/`
- 关键文件：`qemu/nikon-z72.c`（机器）、`qemu/nikon-z72-profile.h`（内存布局）、`qemu/nikon-z72-timer.c`（tick）、`qemu/nikon-z72-counter.c`（计数器）、`docs/architecture.md`（架构）、`docs/porting.md`（21 阶段）、`research/z7ii/z6ii_update_comparison.md`（Z62↔Z72 差异）
