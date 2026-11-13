# Z6 II v1.70：DX45 JPEG / DX30 RAW 目标锁定与补丁工作单

> **最终版（2026-10-29）**：连拍规格见 [BURST_SPEC.md](BURST_SPEC.md)，可执行探针见 [PROBE_DESIGN.md](PROBE_DESIGN.md)，P0/P3 回传修正见 [P0_P3_REANALYSIS.md](P0_P3_REANALYSIS.md)。
> 本轮选择 **B**：运行期 SIF/TG 映射未取到，不生成 DX45/DX30 功能补丁；此前的 `deliverable/` 仅为打包/启动验证包。
> 工具：HMP `memsave`；mas 运行期基址 **0x10020000**（静态 VMA +0x20000）；4K60 在 SLA `0x1091D58C`，mas `0x1092CF50` 视频表无效。
> P0bis 追加：4K60 = 16.667 ms / 497.7 MP/s；P1 启动只有 `0x1032C9C8` 命中 1 次；`0xC2A49000` 对象 arena 全 0，见 P0_P3_REANALYSIS §7。
> **收口（2026-10-29）**：CAPTG 需要实际拍摄序列；LV 是默认 APPLV、Movie/Release/外接快门 G14/G15 在仿真机全部不可达或不触发。P2/P3/P5 标真机，仿真机路线停止；下一轮只在真机有数据时继续。

日期：2026-10-26 · 更新：2026-10-29（K8 验证 / GPIO 修正 / SIF 线索 / A 交付 / B 收尾） · 只读分析，未修改任何原始固件 · 计算脚本：`compute.py` → `budget.json` · 机器可读工作单：`workorder.json`

## 0. 结论（先看这里）

| 目标 | 可行定义 | 读出 | 写卡 | 缓存 | 判定 |
|---|---|---|---|---|---|
| **DX45 JPEG** | **方案 C4：DX 2×2 合并，1968×1312 (2.58MP)，JPEG Fine，45fps** | 116.2 MP/s = 0.34× 线 | 85.5 MB/s，SD UHS-II 可 | 315 帧 ≈ 7.0 s | **CLEAR** |
| DX45 JPEG（10.3MP 全 DX） | 3936×2624 @45 | 464.8 MP/s = **1.36× 线**；编码 1.16× | 315 MB/s，CFe Type B 可 | 85 帧 | **物理否决** |
| DX45 JPEG（5.8MP 输出） | 2944×1968 @45，但仍全 DX 读出 | 464.8 MP/s = **1.36× 线** | 180 MB/s | 150 帧 | **读出否决** |
| **DX30 RAW** | **DX L 3936×2624，30fps，12-bit 无损（或高效）** | 309.8 MP/s = 0.91× 线 | 345 MB/s（无损）→ CFe Type B 400；高效 240 MB/s | 52 帧 ≈ 1.73 s | **CLEAR（12-bit）** |
| DX30 RAW（14-bit 全 DX） | 3936×2624 @30，14-bit | 309.8 MP/s = **1.27× 14-bit 线**（按 12-bit 折算 433.7 MP/s） | 621 MB/s（仅顶规 CFe） | 28 帧 | **物理否决；上限 ≈23.6 fps** |
| DX30 RAW（14-bit，2×2） | 1968×1312 (2.58MP) @30，14-bit | 77.5 MP/s = 0.32× 14-bit 线（按 12-bit 折算 108.5 MP/s） | 156 MB/s | 115 帧 ≈ 3.8 s | CLEAR，但只有 2.6MP |

一句话：**45fps 的 DX 只能走 2.6MP 合并读出；30fps 的全 DX RAW 只能走 12-bit。** 现有菜单已经具备 DX、L/M/S、JPEG/RAW、12/14-bit、Continuous H (extended)，不需要新增菜单项；缺的只有 SIF/TG 运行期配置（窗口/合并/帧周期）。

### 0.1 本轮交付（选择 A：可启动的 K8 重签验证包）

现有静态证据足以做“容器自洽 + 可观测”的签名包，**预期可启动，但启动行为需 QEMU 复核**；不足以做 DX45/DX30 功能包。所以本轮交付的是**打包/启动验证包**，不是功能补丁。功能补丁仍受 SIF/TG 运行期映射阻塞（见 §6）。

| 产物 | 大小 | SHA256 | 说明 |
|---|---|---|---|
| `deliverable/Z6_2_0170_poc_compressed.bin` | 49,002,888 B | `afa1527a3a14068319aa77b00bb6cc15c6d2bcee2a9db3f74daf88cc7e400303` | K8 重签完整容器 |
| `deliverable/eg1850_mas_01700` | 38,750,138 B | `1b42ab7c7399d4b28ca174e6ca64a7cce3c892e831fd929f24c0ad295d1be576` | 打过补丁的 mas（含模块 CRC） |

- 补丁点：mas `+0x1091BF8`，UTF-16 `compressed` → `COMPRESSED`（RAW 菜单标签，20 字节）。
- 预期差异：打开 RAW 记录菜单时显示 `Lossless COMPRESSED`；冷启动健康指标应与原厂一致（客体时间、GPU 上传/渲染、HUD 采样、cpu0 PC 都不应改变）。
- 验证：`python deliverable/verify.py --k8 <K8>`，静态验证全部 PASS（含头部重签、模块/包 CRC、回读解密、差异范围）；启动行为需 QEMU 复核。
- 产物只在本地；未刷机，K8 不写入任何交付文件。

## 1. 关键修正：输出尺寸 ≠ 读出窗口

固件 `mas+0x9417B8` 起的 6 字节记录表是**输出尺寸枚举**，已确认：

| id | 尺寸 | 含义 |
|---|---|---|
| 9 | 3936×2624 | DX L |
| 10 | 2944×1968 | DX M |
| 11 | 1968×1312 | DX S |

但“DX M 输出 2944×1968”不等于“传感器只读 5.79MP”。没有静态证据表明静止画的 M/S 会缩小读出窗口；更常见的实现是全 DX 读出后缩放。因此本报告采用**保守模型**：所有 DX 静止尺寸都按全 DX 10.328MP 读出计；只有显式的 **2×2 合并** 才把读出降到 2.582MP（方案 C4）。

这推翻了早先 `work/dx45/out.txt` 里“DXM 45 CLEAR”的结论——那是把输出尺寸当成了读出窗口。按保守模型：

- DXM 45fps 的**编码器**负载只有 260.7 MP/s（0.65×），不是瓶颈；
- 但**读出**仍是 464.8 MP/s（1.36×），所以不成立。
- 唯一能让 45fps 落在读出线内的 DX 方案，是 2×2 合并后的 2.582MP（116.2 MP/s）。

读出线取两条，均有依据：

- **12-bit：340.72 MP/s** = FX 24.337MP × 14fps（工厂 14fps 点，既有仿真器一直用它）。
- **14-bit：243.37 MP/s** = FX 24.337MP × 10fps。Nikon 官方 Z6 II 规格明确写：`Continuous H (extended): Approx. 14 fps (14-bit NEF/RAW: Approx. 10 fps)`。这就是方案 §四所说“14-bit 读出更慢 ×1.4”的机内证据。

## 2. 预算表（`compute.py` 可复跑）

```
readout line: 12-bit 340.72 MP/s, 14-bit 243.37 MP/s
DX45 JPEG, full-DX readout, L output   464.8 MP/s  315.0 MB/s  pool  85 f/1.89s  CFe      READOUT 1.36x; ENC 1.16x
DX45 JPEG, full-DX readout, M output   464.8 MP/s  180.0 MB/s  pool 150 f/3.33s  SD/CFe   READOUT 1.36x; ENC 1.16x
DX45 JPEG, full-DX readout, S output   464.8 MP/s   85.5 MB/s  pool 315 f/7.00s  SD/CFe   READOUT 1.36x; ENC 1.16x
DX45 JPEG, 2x2 bin readout, 2.6MP      116.2 MP/s   85.5 MB/s  pool 315 f/7.00s  SD/CFe   CLEAR
DX30 RAW, full-DX, 12-bit lossless     309.8 MP/s  345.0 MB/s  pool  52 f/1.73s  CFe B    CLEAR
DX30 RAW, full-DX, 12-bit hi-eff       309.8 MP/s  240.0 MB/s  pool  75 f/2.50s  CFe B    CLEAR
DX30 RAW, full-DX, 14-bit              309.8 MP/s  621.0 MB/s  pool  28 f/0.93s  CFe top  READOUT 1.27x
DX30 RAW, 2x2 bin, 14-bit               77.5 MP/s  156.0 MB/s  pool 115 f/3.83s  SD/CFe   CLEAR
DX 14-bit full-DX ceiling               243.4 MP/s  487.8 MB/s  pool  28 f/1.19s  CFe top  ceiling ≈23.6 fps
```

卡速假设：SD UHS-II 230 MB/s、CFe Type B 400 MB/s、顶规 CFe B 700 MB/s；帧池 600 MB。JPEG 编码器模型 400 MP/s。

### 2.1 输入常量来源与置信度（哪些是实测，哪些是假设）

| 常量 | 取值 | 来源 | 置信度 | 对结论的影响 |
|---|---|---|---|---|
| `LINE12` | 340.72 MP/s | Nikon 官方 `Continuous H (extended) ≈14 fps` × FX 24.337MP；也是 `sim_core_alloc.py` 的工厂基线 | **高（官方规格，未在本机计时）** | DX30 的 309.8 MP/s = 0.91×；若真实线低 10%，就变成 1.01×，DX30 会从 CLEAR 变边际 |
| `LINE14` | 243.37 MP/s | Nikon 官方 `14-bit NEF/RAW ≈10 fps` × FX 24.337MP | **高（FX 官方）/ 中（外推到 DX）** | 14-bit 全 DX @30 = 1.27×，即使线高 10% 仍否决；结论稳健 |
| `ENC` | 400 MP/s | **假设**；唯一硬点是现役 FX 14fps JPEG = 340.7 MP/s | **低–中** | C4 编码负载 116.2 MP/s = 0.29×；即使编码器只有 200 MP/s 也够，结论稳健 |
| `POOL` | 600 MB | **假设**，未测得 | **低** | 只影响连拍时长（帧数/秒数），不影响 fps 可行性 |
| `CARDS` | 230 / 400 / 700 MB/s | 卡等级典型值，未在本机实测 | **中** | DX30 无损 345 MB/s 需要 CFe Type B；若卡只有 300 MB/s，持续帧率会掉 |
| `PAY` | 见 `compute.py` 表 | **估算**：分辨率 × 位深 × 典型压缩率，未从实际文件统计 | **低–中** | JPEG 1.9 MB/帧即使翻倍到 3.8 MB，仍 171 MB/s < SD 230；但 RAW 无损 11.5 MB/帧若涨到 16 MB，就是 480 MB/s > CFe B 400，持续段会掉 |
| 497 MP/s | — | 附件 §〇 的 4K60 DX 标定 | **中（外部推算）** | 只作为 TG 能力参考，没有当作静止画硬上限；本报告的硬线用 LINE12/LINE14 |

**明确：`ENC`、`POOL`、`CARDS`、`PAY` 都是模型输入，不是本机实测。** `LINE12/LINE14` 有官方规格支撑，但也没有在本机做读出计时；`497 MP/s` 来自附件的外部标定。所有 fps 结论都是“在这些假设下的可行性判断”，不是实测结果。

## 3. 两个目标的定义

### 3.1 DX45 JPEG = 方案 C4

- 读出：DX 2×2 合并，1968×1312 = 2.582MP；45fps → 116.2 MP/s。
- 输出：JPEG Fine 1968×1312（约 1.9 MB/帧），85.5 MB/s；SD UHS-II 足够。
- 缓存：315 帧 ≈ 7.0 s（600 MB 池）。
- 需要的固件改动：SIF/TG 合并/窗口配置、CH extended 帧周期 22.22 ms、ISP 输出 1968×1312、JPEG 参数。
- 不需要新增菜单/字符串：现有菜单已有 DX、Image size S、JPEG fine、Continuous H (extended)。
- 风险：45fps 超过机械快门能力，必须复用电子快门路径；方案把“跳过机械快门”标为 S 类高风险，但 Z6 II 的 14fps extended 本身就是电子快门路径，因此**不必新写快门控制流**，只需把该路径的周期压到 22.22 ms。这是本方案最大的风险削减点，但仍需真机确认 14fps extended 的实际快门路径。

### 3.2 DX30 RAW = 方案 C3（12-bit）

- 读出：全 DX 3936×2624 = 10.328MP；30fps → 309.8 MP/s（0.91× 12-bit 线）。
- 输出：12-bit 无损约 11.5 MB/帧 → 345 MB/s；CFe Type B 400 MB/s 可持续；600 MB 池约 52 帧 ≈ 1.73 s。
- 高效 RAW：约 8.0 MB/帧 → 240 MB/s；SD UHS-II 230 略超，建议 CFe。
- 需要的固件改动：SIF/TG 窗口 3936×2624 @30、CH extended 周期 33.33 ms、RAW 12-bit/无损、ISP RAW 输出。
- 不需要新增菜单：现有菜单已有 DX、Image size L、NEF (RAW)、Lossless compressed、12-bit、Continuous H (extended)。
- 14-bit 不在本目标内：全 DX 30fps 14-bit 读出 309.8 MP/s = **1.27× 14-bit 线**（按 12-bit 折算 433.7 MP/s），官方规格也把 14-bit 限制在约 10fps（FX）。若必须 14-bit + 30fps，只能降到 2×2 合并的 2.6MP（156 MB/s），或等一个不存在的 5.8MP 读出模式。

## 4. 固件证据（可复现）

| 地址（file off / VMA） | 内容 | 用途 |
|---|---|---|
| `0x9417B8` / `0x109417B8` | 6B 记录 `(u16 w,u16 h,u16 id)`；id 9/10/11 = DXL/DXM/DXS | 输出尺寸枚举；**不是读出窗口** |
| `0x92CF50` / `0x1092CF50` | 18 条 ×12B `[u16 id][u16 0][u32 fps][u16 w][u16 h]` | 视频读出模式表；最高 248.8 MP/s；无 45fps、无静止 DXL |
| `0x1091900` 起（UTF-16） | `NEF (RAW)+JPEG fine/normal/basic`、`JPEG fine/normal/basic` | 画质菜单已具备 |
| `0x1091B00` 起（UTF-16） | `Large/Medium/Small`、`5:4 (30x24)/FX/DX/DX (24x16)`、`Lossless compressed/Compressed`、`12-bit/14-bit` | 画幅/尺寸/压缩/位深菜单已具备 |
| `0x1092D14` 起（UTF-16） | `Single frame/Continuous/Continuous H/Continuous L` | Release mode 菜单 |
| `0x10A3378` 附近（UTF-16） | `Continuous H (extended)` | 现有 14fps 电子连拍路径的 UI 锚点 |
| `0x242E...` | 任务表：`CAPSEQ` @0x242ED50、`CAPTG` @0x242ED6C、`SHUTTER` @0x242ED88、`STGRAW` @0x242E9EC | TG/快门/RAW 任务存在 |
| `0x119DF50C` | 疑似拍摄模式记录块，步长 0x480；每条仅 3–7 个非零字节 | 静态无模式参数；早先“全零”说法应修正为“近乎全零” |

### 4.1 JPEG 编码器证据（DX45 线的编码侧）

| 位置 | 内容 |
|---|---|
| mas `0x242DF18` / `0x242DF34` | 单元名 `IMPJPGCO` / `IMPJPGEN`，记录步长 0x1c |
| sla `0x852024` / `0x852040` | 同名单元 `IMPJPGCO` / `IMPJPGEN` |
| sla `0x1485F6` / `0x148616` / `0x148696` | `JPEG_CODEC` / `JPEG_ENC` / `H264` |
| sla `0x8307C..0x83158` | `ENCEVENT_H264BUFFER0_ENCODED` … `ENCEVENT_ENCODE_END`（编码完成事件） |

编码负载对比：

- 现役 FX 14fps JPEG = 24.337MP × 14 = **340.7 MP/s**（已知可用，是硬证据）；
- C4 目标 = 2.582MP × 45 = **116.2 MP/s**，只有现役的 **0.34×**。

所以 45fps JPEG 的瓶颈不在编码器，只在 TG 读出配置与电子快门路径。

### 4.2 0x2404xxxx 不是 TG，是 GPIO/引脚控制（重要修正）

附件 §七把 `0x2404xxxx` 当作 TG/采集寄存器。真实 mas 确实访问这个区间，但代码形态是 GPIO：

- 字面量池 mas `0x450674..0x450AA8`（VMA `0x10450674` 起）给出 `0x24040000`、`0x24040100`、`0x24042000`、`0x24042100`、`0x24045000..0x24045300`、`0x24046000/0x24046100`，另有 `0x20140000/0x20141000/0x2014D000` 等。
- 三个批量写函数 `0x450320 / 0x4504B6 / 0x450578` 都是 **16 次循环（每次 2 项，共 32 个引脚）**，每次写 8-bit 或两个 12-bit 字段，对应输入/输出/上下拉配置；`0x4509EE` 是对应的回读函数，用 `ubfx` 取 bit1/bit9/bit16/bit24。这是 GPIO 端口的典型形态，不是帧时序。
- 同一驱动里的 `0x2014xxxx` 是 GPIO/引脚复用的时钟/复位寄存器。
- 结论：**按附件 §7 记录 `0x24040000..0x24060000` 只会抓到 GPIO 配置，抓不到 TG 帧周期。探针范围必须改。**

### 4.3 真正的传感器接口线索：SIF_0..SIF_7

- IP 名表 mas `0x76B010`，步长 0x20，前 31 项为：`EXT_INT_9..15`、`CA7_AXI_ERROR`、`SIF_0..SIF_7`、`PPC_0..PPC_4`、`MIPU_0/1`、`IPU`、`IIP`、`IDMAC_0/1`。
- 同序处理表 mas `0x2EE634`（VMA `0x102EE634`），31 个 Thumb 指针；第 12–19 项正好是 `SIF_0..SIF_7`，处理函数在 `0x1030C77F..0x1030CC67`。
- 这些处理函数是 SIF 服务的消息处理（读消息结构、调下层 HAL），不直接写 MMIO；寄存器读写在其下层。
- 组件表 mas `0x242C224` 给出：`ASifDriver=0x41090000`、`SifService=0x421D0000`、`CaptureSensorService=0x02020000`、`CaptureSequenceService=0x04210000`。
- 结论：下一轮探针应围绕 **SIF 服务处理函数（0x1030C77F..0x1030CC67）与传感器 I2C**，而不是 `0x2404xxxx`。

### 4.4 K8 已验证，签名链闭合

- K8（用户提供，已存项目记忆，不在本文件复述）；`SHA1(body || K8)` = `89019167cb702344a1afa1d44e408f26b259d3e4`，与头前 20 字节逐字节相同。
- `z6ii_patch.py --selftest --k8 ...` 对官方 `.bin` 零改动重打包，**差异 0 字节**。
- K8 按机型/版本独立：Z7 II 1.70、Z50 2.60、Z6 3.80、Z8 3.11 均不匹配。
- 影响：等长限制解除；任意载荷可走 **模块 CRC16 → 包 CRC16 → SHA1(body||K8) → XOR 再加密** 全链重封；若改模块长度，必须同步更新目录项 `off/len`。
- PoC：`deliverable/Z6_2_0170_poc_compressed.bin`，把 RAW 菜单 `Lossless compressed` 改成 `Lossless COMPRESSED`，SHA256 `afa1527a3a14068319aa77b00bb6cc15c6d2bcee2a9db3f74daf88cc7e400303`；仅本地副本，未刷机。

已排除的伪锚点（避免重走弯路）：

- `0x92F648`：不是帧率阶梯，是 `30 + 偶数 10..62 + 奇数 9..61` 的数值表。
- `0x1BF2586`：u16 `14,12,10,8,6,5,4,3,2,1` 只是一个长倒数表的尾段，14 是碰巧。
- `0x2291FB4`：f32 `2..256` 是通用数值梯（1/3 stop 风格），不是 fps 表。
- 在 `3936×2624` / `2944×1968` / `1968×1312` 的全部数据命中附近，没有相邻的 14/30/45 帧率字段；静止画读出模式不是一张静态可改的表。
- `33333` / `22222` 作为 u32 全镜像 **0 命中**；`14286` 的命中落在 Thumb 代码字节里，不是周期常量。
- `0x23C5D80` / `0x23B5EC0` 附近的 f32 `14/30/45` 命中是普通浮点数据区里的巧合（已逐字 dump，周围是 -16.x 的系数），不是帧率表。
- `0x9CBD68` / `0xA15E08` 的 u32 `71428` 是 6 字节传感器寄存器记录 `(u16 count=1, u16 reg=0x5983, u16 value=0x1704)` 跨读出来的，不是 14fps 周期。
- `0x19DBA60` 的 `200000` 属于快门速度表（0.2 s），不是连拍周期；同表还有 100000/130000/150000/250000/300000/600000。

## 5. 补丁工作单

### 5.1 已确认可做

- 不改菜单、不改字符串、不改画质/位深枚举；这些都已存在。
- 容器级补丁 + 模块 CRC/包 CRC 重算已有工具（`z6ii_patch.py`）；K8 已验证，头部 SHA1 可重签。
- 目标模式参数已经算死：DX45 = 2×2 1968×1312 @45；DX30 = 全 DX 3936×2624 @30 12-bit。

### 5.2 仍缺的唯一一组值

TG/采集子系统的**运行期**配置：

1. SIF/TG 基址与帧周期：`0x2404xxxx` 已确认是 GPIO，不再是候选；需要从 SIF 服务处理函数（`0x1030C77F..0x1030CC67`）向下追到其 HAL 的 MMIO 基址。
2. 静止画 DX 窗口寄存器（3936×2624）。
3. 2×2 合并窗口寄存器（1968×1312）。
4. CH extended 帧周期寄存器（33.33 ms / 22.22 ms）。
5. 12-bit RAW 的读出/打包配置（与 14-bit 的差异）。
6. ISP 输出尺寸/RAW 打包配置。

静态镜像里这些值不在可改的常量表中：`0x119DF50C` 记录几乎全零，视频表无静止项，任务表只有描述符没有入口指针；`0x2404xxxx` 已确认是 GPIO，不是 TG。这与附件 §7 的判断一致——**必须先拿到运行期 SIF/TG 映射，离线不能验证**。

### 5.3 真机 5+3 组探针（按附件 §7 扩展）

记录 SIF 服务处理函数及其下层 HAL 的 MMIO 写入：偏移、值、调用者 LR；同时记录传感器 I2C 事务和当前画幅/尺寸/画质/位深/释放模式。`0x24040000..0x24060000` 已确认是 GPIO，不再作为探针目标。

| # | 场景 | 目的 |
|---|---|---|
| 1 | 单张 FX 12-bit | 基线 |
| 2 | CH 5fps FX | 低速周期 |
| 3 | CH extended 14fps FX 12-bit | **14fps 周期基准** |
| 4 | CH extended 14fps FX 14-bit | 量化 ×1.4 位深代价 |
| 5 | 视频 4K30 DX | DX 窗口 + 30fps 周期参考 |
| 6 | 视频 4K60 DX | 60fps 周期/窗口参考 |
| 7 | 静止 DX L 14fps 12-bit | **全 DX 读出基准** |
| 8 | 静止 DX M / S 14fps | 判断 M/S 是否缩小读出窗口（关键：若轨迹与 L 相同，证明输出尺寸≠读出窗口） |

探针优先 hook SIF 服务处理函数 `0x1030C77F..0x1030CC67`（表 file `0x2EE634` 第 12–19 项）及其下层 HAL，记录 `(ctx, base, offset, value, LR)`；同时抓传感器 I2C。GPIO 那组函数（`0x10450320 / 0x104504B6 / 0x10450578`）只作为对照，不再作为 TG 候选。

差分方法：

- #3 vs #5/#6 → 帧周期寄存器与时钟分频；
- #7 vs #8 → 读出窗口是否随输出尺寸变化；
- #3 vs #4 → 14-bit 的额外行/时钟；
- #5/#6 → 2×2 合并/窗口配置的参考值。

### 5.4 补丁模板（拿到寄存器值后填）

```
目标 A：DX45 JPEG
  TG: 2x2 bin window 1968x1312, period 22222us (45fps)
  ISP: out 1968x1312, JPEG Fine
  复用现有 CH extended / 电子快门路径
目标 B：DX30 RAW
  TG: window 3936x2624, period 33333us (30fps), 12-bit
  ISP: RAW 12-bit lossless/hi-eff
  复用现有 CH extended / 电子快门路径
容器：
  替换 SIF/TG 配置块（可不等长）→ 重算模块 CRC16 → 重算包 CRC16
  → SHA1(body || K8) 头部重签 → key.json XOR 再加密
  若改模块长度，同步更新目录项 off/len；刷机仍需授权与恢复预案
```

### 5.5 验证顺序

1. 静态：模块 CRC、包 CRC、解包/再加密自洽；`z6ii_patch.py --selftest --k8 ...`（差异必须 0）。
2. 差分：补丁只改目标 SIF/TG 块，其他模块字节零差异。
3. 真机：先拍单张，再 5fps，再目标模式；每步检查黑屏、ERR、卡写、温度。
4. 不在未授权情况下刷机；不修改原始 `.bin/.dec`。

## 6. 风险与未确认项

- **45fps 电子快门路径**：现有 14fps extended 是否为纯电子快门，需真机/探针确认；若不是，方案 §六的 S 类“跳过机械快门”仍然是高风险项。
- **2×2 合并的画质**：电荷相加理论上更干净，但 45fps 下的 TG 时序、黑电平、暗电流需实测。
- **14-bit 目标**：全 DX 30fps 14-bit 已被读出线否决；不要在补丁里尝试把 12-bit 时序直接套到 14-bit。
- **卡**：DX30 RAW 无损 345 MB/s，必须 CFe Type B（400+）；SD UHS-II 只能撑约 20fps 持续。
- **探针范围已修正**：附件 §七的 `0x2404xxxx` 实际是 GPIO（§4.2）；真正的线索是 SIF_0..SIF_7（§4.3）。不要再在 GPIO 上做帧周期差分。
- **离线不可验证**：附件 §7 的 CAPTG 前提未解决；没有真机 TG 轨迹，任何“已攻破”的说法都不成立。
- **K8 已解决**：`SHA1(body || K8)` 已验证，零改动重打包差异 0 字节；打包链闭合。K8 与固件属敏感材料，不要提交到 git 或对外文档；刷机仍需授权与恢复预案。
- **交付物是验证包，不是功能包**：本轮 A 选项只验证 K8 重签 + 容器启动 + 菜单可观测差异；DX45/DX30 的功能补丁仍未生成。

### 6.1 仍未证实的寄存器/表/时序

| # | 项 | 现状 | 为什么没证实 |
|---|---|---|---|
| 1 | SIF/TG 基址与窗口寄存器（3936×2624、1968×1312） | 只定位到 SIF_0..7 的消息处理函数；下层 HAL 未追到 MMIO | SIF 处理函数不直接写 MMIO；静态镜像里该区域没有直接地址字面量 |
| 2 | 帧周期寄存器（33.33 ms / 22.22 ms） | 未找到 | `33333`/`22222` 作为 u32 全镜像 0 命中；视频表只有输出模式，不含静止 DXL |
| 3 | 12-bit 与 14-bit 的读出/打包差异 | 只有官方 14fps/10fps 的 ×1.4 推断 | 没有寄存器级证据 |
| 4 | 2×2 合并模式 | 只有 C4 的几何/预算推断 | 没有静态配置或真机轨迹 |
| 5 | 14fps extended 是否纯电子快门 | UI 字符串存在，路径未确认 | 需要真机/探针确认 |
| 6 | 帧池 600 MB、编码器 400 MP/s、PAY 表 | **模型假设** | 未在本机实测（见 §2.1） |
| 7 | SD 升级路径是否校验头部 SHA1 | 有“CRC-only 分支”的代码结构线索 | 未在真机/下载态验证 |

### 6.2 最小验证实验清单

最终版拆成 6 个可执行探针，详细地址/条件/场景/签名/回传格式见 [PROBE_DESIGN.md](PROBE_DESIGN.md)：

1. **P0 启动基线**：dump 任务表 `0x1242E7A0`（60×0x1C）、六条模式记录 `0x119DF50C`（6×0x480）、组件表。
2. **P1 SIF 入口**：hook `0x1030CC66 / 0x1030CBCA / 0x1030CB8E / 0x1030CB18 / 0x1030CABE / 0x1030C9C8 / 0x1030C882 / 0x1030C77E`，dump `r0/r1/[r0+0xC]`。
3. **P2 模式记录写监视**：`0x119DF50C..0x11A0100C`。
4. **P3 任务表运行期副本**：在 RAM `0x10000000..0x14000000` 搜 `CAPTG/CAPSEQ/SHUTTER/STGRAW` 名字，找代码入口指针。
5. **P4 表读监视**：视频表 `0x1092CF50..0x1092D028`、输出尺寸表 `0x109417B8..0x109417F8`。
6. **P5 MMIO 写回退**：`0x20000000..0x25000000`，排除 GPIO 区间，只记写并做场景差分。

通过标准见 [BURST_SPEC.md](BURST_SPEC.md) §4；拿到映射后才按 §5.4 生成功能补丁，再走单张 → 5fps → 目标模式的台架验证。

## 7. 外部依据

- Nikon Z 6II 官方规格：Continuous H (extended) Approx. 14 fps；**14-bit NEF/RAW: Approx. 10 fps**。
  - https://imaging.nikon.com/imaging/lineup/mirrorless/z_6_2/
  - https://www.nikon-asia.com/z-6ii-body-only
- 方案文档：`C:\Users\BI\Downloads\连拍提速-全方案整合-20261023.md`（C3/C4、§四 14-bit ×1.4、§七 CAPTG 前提、§九 真机探针）。
