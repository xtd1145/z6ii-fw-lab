# Z6 II 预拍摄 · 固件侧实现说明（探针版 v3）

状态：钩子层 + 状态机 + **JPEG 数据面探针**已实现并签名出包。
**数据面（帧/码流截留）尚未实现** —— 静态证据不足以定位切点，原因见 §2，取证清单见 §9。

| 产物 | 说明 |
|---|---|
| `work/preshoot_build/Z6_2_0170_preshoot_probe.bin` | **本次交付**：49,002,888 B，K8 重签，包 CRC `cf9a`，头摘要 `c5e4e26b…` |
| `work/preshoot_build/Z6_2_0170_preshoot_dram.bin` | 上一版：状态在 DRAM，无探针；QEMU 冷启动 PASS（246 事件） |
| `work/preshoot_build/Z6_2_0170_preshoot.bin` | 已废弃（状态在只读镜像 → Data Abort） |
| `work/preshoot_build/Z6_2_0170_hooks.bin` | 只有钩子层，QEMU PASS，保留备查 |

尺寸口径：模块完整长度 **38,750,138 B**（含尾 2 B CRC），payload 38,750,136 B，均未变。

---

## 1. 规格可行性表

几何与帧率：FX 6048×4024 = 24.34 MP，DX 3936×2624 = 10.33 MP，14 fps。
RAW 未压缩 = 1.5 B/px（12-bit）/ 1.75 B/px（14-bit）；无损压缩比 0.6–0.7（内容相关）。
JPEG = 0.15–0.25 B/px（**内容相关，不能静态确定**）。

### 1s JPEG（14 帧）

| 档位 | 单帧 | 14 帧 | 写卡带宽 | 应用堆 37.25 MB | 卡环形 | 帧池截留 |
|---|---|---|---|---|---|---|
| FX | 3.6–6.1 MB | **50–85 MB** | 50–85 MB/s | ✗ | **✓** | 需实证 |
| DX | 1.5–2.6 MB | **21–36 MB** | 21–36 MB/s | ✗（贴近上限，会挤爆其它分配） | **✓** | 需实证 |

### 0.5s RAW（7 帧）

| 档位 | 单帧 | 7 帧 | 写卡带宽 | 堆内 | 卡环形 | 帧池 |
|---|---|---|---|---|---|---|
| FX 12-bit 未压缩 | 36.5 MB | 255 MB | 511 MB/s | ✗ | ✗ | 需实证 |
| FX 12-bit 无损 | ~23 MB | ~161 MB | ~322 MB/s | ✗ | ✗ | 需实证 |
| FX 14-bit 无损 | ~27 MB | ~189 MB | ~378 MB/s | ✗ | ✗ | 需实证 |
| DX 12-bit 未压缩 | 15.5 MB | 108 MB | 217 MB/s | ✗ | △ | 需实证 |
| DX 12-bit 无损 | ~10 MB | ~70 MB | ~140 MB/s | ✗ | △ | 需实证 |
| DX 14-bit 无损 | ~11.5 MB | ~81 MB | ~161 MB/s | ✗ | △ | 需实证 |

（写卡带宽参考：UHS-II V90 卡在 Z6 II 上的持续写约 150–200 MB/s。）

**判定**
- **可行**：`1s JPEG FX`（写卡 50–85 MB/s，余量最大）、`1s JPEG DX`（21–36 MB/s，最省）
- **否决**：所有 `0.5s RAW` —— 即便 DX 压缩后也要 ~140 MB/s 持续写，超出可靠区间；FX 全部 ≥320 MB/s
- **否决**：所有堆内方案（最大 36 MB ≈ 37.25 MB 上限，无余量）
- **未定**：帧池截留容量上全部可行（Z6 II 连拍规格证明帧池能缓存上百帧 RAW），但**缺帧池接口证据**

选定档位：**1s JPEG FX 走卡环形**；若求稳可退到 DX（同一实现，只改尺寸策略）。

## 2. 数据面为什么没有实现（实证记录）

六条相互独立的否定证据：

1. **现有 8 个钩点都不携带帧指针**。逐个核查：`CapStQue` 是 32 B payload 的状态队列
   （80 项 × 36 B），`FrontEnd-A` 是闪光 TTL 测光，`CamCtl-InitiateCapture` 是批量拍摄计数日志。
2. **文件名模板 `DSC_0000.JPG`**（`f20981b8`）只被 2 张 rodata 指针表引用，无代码直接引用。
3. **`EncodeParamKey` / `EncodeContextKey`**（`f208d25c/6c`）同样只出现在键表里。
4. **`FILESRV / FILEWM / FILEWP / FILECOM`**（`f242dc08…`）零 dword 引用 —— 是枚举名表。
5. **Z6 II 没有 `SyscExternalAccessor.c`**（0 命中），Z6 上 WL 用过的文件服务钩点无法搬迁；
   `ReleaseStateControl.c` / `GCaptureStateControlServiceStd.c` 同样不存在。
6. **编码很可能在副核**：`JPEG_CODEC` / `JPEG_ENC` / `EncodeParamKey` 在 mas 与 sla **同时存在**，
   说明编码服务是双核接口；若 JPEG 在 sla 生成、只把结果交回 mas，mas 侧根本看不到码流产生过程。

因此没有硬造一个"假的数据面"。取而代之：**把探针做进核心**，让运行期自己指出切点（§7）。

## 3. 布局

镜像内（只读，纯代码/常量）：

| 区段 | 文件偏移 | VMA | 大小 |
|---|---|---|---|
| CTRL | `f000e01c` | `0x1002e01c` | 16 B |
| FTAB | `f000e030` | `0x1002e030` | 32 B |
| DISPATCH | `f000e080` | `0x1002e080` | 88 B |
| THUNK×8 | `f000e100` | `0x1002e100` | 18 B/个 |
| FALLBACK×8 | `f000e200` | `0x1002e200` | 8–14 B/个 |
| CORESLOT | `f000e2c0` | `0x1002e2c0` | 表 32 B + 代码 224 B |
| COREWORK | `f000e3e0` | `0x1002e3e0` | **354 B** |

镜像内共 1318 B / 2470 B 干净区；整模块与原厂差 895 字节。

运行期（DRAM，可写）：

| 区段 | 地址 | 大小 |
|---|---|---|
| STATUS | `0xC5EFF000` | **64 B** |
| RING | `0xC5EFF100` | 768 B（48 × 16 B） |
| 保留 | `0xC5EFF400`–`0xC5F00000` | 3 KiB |

地址依据同上一版：实验机确认 `0xC2C00000..0xC5F00000` 为运行期可写 RAM，取最后一个 4 KiB。
风险也与上一版相同：与 heap 共享，被覆盖时靠 magic+补码检测并重新初始化。

## 4. CTRL（`0x1002e01c`，16 B，只读）

| 偏移 | 字段 | 值 | 含义 |
|---|---|---|---|
| +0x00 | magic | `0x5043365A`（'Z6PC'） | 清零 = 不安装核心 |
| +0x04 | core_base | `0x1002e2c0` | 核心基址 |
| +0x08 | n_slots | `8` | 槽数 |
| +0x0c | reserved | `0` | — |

入口表在 `core_base + 0x10`，每项为相对 core_base 的偏移。
槽入口 ABI：进入时 r0–r3/lr/flags 与钩点一致，r12 已被派发器占用；返回时跳到 `FTAB[slot]`。

## 5. STATUS（`0xC5EFF000`，64 B，DRAM）

| 偏移 | 字段 | 含义 |
|---|---|---|
| +0x00 | magic | `0x5350365A`（'Z6PS'），最后写入 |
| +0x04 | count | 累计事件数 |
| +0x08 | write_idx | 下一个描述符位置（模 48） |
| +0x0c | frozen | 1 = 已冻结 |
| +0x10 | retained | 冻结时保留数 = `min(count,48)` |
| +0x14 | triggers | 触发次数 |
| +0x18 | armed | 初始化即 1 |
| +0x1c | magic_inv | `0xACAFC9A5`（magic 补码，双校验） |
| +0x20 | **jpeg_hits** | r0/r1 命中 JPEG SOI 的累计次数 |
| +0x24 | **jpeg_slot** | 首次命中的 slot，`0xFFFFFFFF` = 未命中 |
| +0x28 | **jpeg_addr** | 首次命中的缓冲区地址 |
| +0x2c–0x3f | reserved | 0 |

初始化协议：magic 与补码都对才认为有效；否则先写补码 → 清零 → armed=1 → **最后写 magic**，
中途被打断不会被误认为已初始化。

## 6. 静态 MMU 检查（构建期门禁）

`hookgen.check_runtime_writes` 反汇编派发器、槽入口、状态机，跟踪 `movw/movt` 与移位 `add`
的地址传播，断言每条 store 的目标不在 `0x10020000..0x12520000`；基址不可解析即失败。

```
dispatcher   0 store(s) proven outside 0x10020000..0x12520000
core_slot    0 store(s) proven outside 0x10020000..0x12520000
core_work   27 store(s) proven outside 0x10020000..0x12520000
```

`check_state_place()` 另行断言 STATUS/RING 整体落在可写窗口且在镜像之外。

## 7. JPEG 探针（本版新增）

每次钩点调用时，核心检查**两个参数**（r0、r1）：

1. 地址必须落在 `0xC2C00000..0xC5F00000`（已确认映射的窗口）——**只在窗口内解引用，因此不会 fault**；
2. 4 字节对齐；
3. 首字低 24 位等于 `0xFFD8FF`（JPEG SOI，小端）。

命中则 `jpeg_hits++`；首次命中额外记录 `jpeg_slot` 与 `jpeg_addr`。

**用途**：如果 QEMU 里跑一次拍摄后 `jpeg_hits > 0`，`jpeg_slot` 就直接告诉你
**哪个钩点携带 JPEG 码流**，那就是数据面切点；`jpeg_addr` 给出缓冲区地址。

**已知局限**（如实写明）：
- 缓冲区若在窗口外则漏检（为了不 fault，只能如此）；
- 若 JPEG 由 sla 生成、mas 只持有句柄而不持有数据指针，则永远不命中；
- 理论上其它数据可能偶然匹配 `FF D8 FF`，但需同时满足"窗口内 + 对齐 + 首三字节"，概率极低。

## 8. QEMU 冷启动复测判据

读 `xp/16wx 0xc5eff000`：

| # | 观察 | 期望 |
|---|---|---|
| 1 | 整体 | PASS：guest ≥2.4 s、上传 ≥250、渲染 ≥200、HUD >30、cpu0 不在 `0x101034fc` |
| 2 | `[0xc5eff000]` | `0x5350365a` |
| 3 | `[0xc5eff01c]` | `0xacafc9a5` |
| 4 | `[0xc5eff018]` | `1`（armed） |
| 5 | `[0xc5eff004]` | count ≥ 0（上一版冷启动观测到 246） |
| 6 | `[0xc5eff020]` | **jpeg_hits**；若 QEMU 能跑拍摄，这是本次最关键的读数 |
| 7 | `[0xc5eff024]` / `[0xc5eff028]` | 首次命中的 slot / 地址 |
| 8 | Data Abort 到 `0xc5effxxx` 或 `0x1002e2xx` | FAIL |

## 9. 如果探针仍未命中：最小实验清单

按顺序做，每步都能单独给出结论：

1. **确认 QEMU 能走到拍摄**。触发一次拍摄（按键或注入命令），看 `count` 是否增长。
   若 count 不变 → 这 8 个函数不在 QEMU 的拍摄路径上，需先扩展 QEMU 的拍摄流程或换真机。
2. **拍后立即 dump `0xC2C00000..0xC5F00000`**（QEMU `pmemsave`），离线搜 `FF D8 FF`。
   找到 → 记下地址与出现时刻，再用 `xp` 在拍摄前/后对比，锁定是哪个分配区间。
3. **在该页设写监视**（gdb `watch *0xADDR`），抓第一个写入者的 PC。
   那个 PC 就是"JPEG 产出点"，把它做成第 9 个钩点即完成数据面。
4. **若 mas 侧始终看不到码流**：说明编码在 sla。此时需要 sla 侧证据：
   dump `0x12520000` 段，或对 sla 核做同样的探针（现有钩子层机制可直接复用，
   只需把 `MAS_BASE` 换成 `SLA_BASE`、重新选钩点）。
5. **帧池路线另需一条证据**：帧缓冲的分配/回收函数。当前证据显示它不在 mas 的应用分配器里
   （473 个分配点中静态最大 94 KB），所以要么在 sla，要么在 boot 层 —— 需运行期 dump 确认。

## 10. 复现与验证

```bash
python work/preshoot_build/hookgen.py        # 布局 + 静态 MMU 门禁 + patch_spec.txt
python work/preshoot_build/test_core.py      # A / B1 / B2 / B3 / B4
python work/preshoot_build/build_pkg.py      # 出包（默认 *_preshoot_probe.bin）
python work/preshoot_build/verify_patch.py   # 容器级复核 + 在容器载荷上重跑 A/B
```

`test_core.py` 判据：
- **A**（CTRL 清零）8 站点 × 48 组随机状态，退出地址与 r0–r11/sp/lr/NZCV 等同原厂；
- **B1**（核心启用）同上；
- **B2** 状态机：magic、armed、count、描述符内容、冻结、冻结后不追加、回绕；
- **B3** 状态块预填全 1 / 随机 / 仅 magic，都必须被正确重新初始化；
- **B4** 探针：r0 命中、r1 命中、垃圾不命中、窗口外不命中且不 fault、非对齐不命中。

刷入相机属高风险操作，未执行，也不应在本 lab 内执行。

## 11. 拍摄触发路径：仿真机上为什么按不出照片

**结论：快门不在按键扩展器的 24 pin 里，它走 ExMCU 通道；QEMU 目前没有这个通道。**

### 11.1 证据

mas 里存在完整的 ExMCU 服务面，而按键矩阵只覆盖 UI：

| 名字 | 位置 | 含义 |
|---|---|---|
| `ExMcuEvt` / `ExMcuFL` | `f01e651c` / `f01e6530` | ExMCU 事件 / 标志 |
| `ExMcuCommDebug` | `f01ebeac` | ExMCU 通信调试 |
| `ExMcuService` / `ExMcuCommandServ` / `BExMcuProtocolSer` | `f242c32c` / `f242c794` / `f242cc5b` | 服务 / 命令 / 协议对象 tag |
| `EXMCUSRV` / `EXMCUFUP` | `f242f044` / `f242f060` | ExMCU 服务 / 固件升级通道 |
| `eSyscCommandId_BSSendExMcuCommand` | `f2096334` | 向 ExMCU 发命令的 Sysc 事件 |

按键侧只有 UI 键位表：`ID_ROTSW1_COMMAND_DIAL_SUB`、`ID_SW1_LIVEVIEW`、`ID_SW2_SS_RIGHT`…
（`f0918b91` 起）—— `SW1/SW2` 在这里是**旋转开关/滑动开关**，不是快门半按/全按。
实测 24 pin 中 8 个能产生钩子事件（1/2/11/14/17/19/21/23），正是 UI 键位；
而 `pmemsave` 整个 51 MiB heap 搜不到一个 `FF D8 FF`，说明**根本没有生成过 JPEG**。

### 11.2 拍摄执行链（已定位，可用于 trace）

| 地址 | 证据 | 作用 |
|---|---|---|
| `0x1076b4e0` | 内含 `"ShutterDrive request:%d resultCount:%d"` (`f074b74c`) | **快门驱动请求**；参数 `(r0, r1, r2)` 被拼成 `{u16, u32}` 消息后交给 `f074b4e0+0x1c` 的发送函数 |
| `0x1076b512` | `ldr r1,[r0,#4]` 后按值分发 | 命令分发候选：`[r0+4]` 为 **6** 或 **0x20** 时走同一分支 |
| `0x1076910c` | 内含 `"Release was failed/timeout"` (`f0749608/f0749620`) | **InitiateCapture**（本补丁 slot 7 已钩） |
| `0x1074c060` | 内含 `"Wait ReleasePermission up to %d sec."` (`f074c214`) | 等释放许可；`[r0+4]` 是等待秒数，随后经函数指针 `[r0+8]` 调用 |
| `0x1076930a` | `movs r0,#1; movw r2,#0xea60; bl 0x10524016` | 60 000 单位超时等待，返回位为 1 才继续 |
| `0x10749fd6` / `0x1074a028` | `"InspectReleaseFD/LV finished. request:%d current:%3d"` | FD（全按）/ LV（实时取景）释放检查 |

**最小探测方案**（QEMU 侧，按顺序）：

1. 在 `0x1076b4e0`、`0x1076910c`、`0x1074c060` 设函数入口 trace（只需 PC 命中即记录参数）。
2. 冷启动后按任意 pin，同时观察三者是否命中：
   - 都不命中 → 按键没有进入拍摄模式（当前情况）；
   - `0x1074c060` 命中但之后中止 → 卡在"释放许可"，见 §12。
3. 若要在 QEMU 里**主动**触发一次：优先伪造 `ReleasePermission` 的许可条件，而不是直接调 `0x1076b4e0`
   （后者的 `{u16,u32}` 参数语义未证实，误调可能把状态机带进不一致态）。

## 12. 拍摄前置的"就绪"标志

固件以 Sysc 事件的形式等待这些外部设备就绪（枚举名取自 mas 的 `eSyscCommandId_*` 表）：

| 事件 | 相关设备 |
|---|---|
| `CardFirstMountReqested` / `CardMountComplete` / `CardMountError` | SD 卡 |
| `FLensConnectComplete` / `MlLensConnectComplete` / `LensConnectCheckComplete` | 镜头（F / ML 两路） |
| `ConnectDefaultDevice` / `ExtSbConnectComplete` | 外部设备 |
| `AppFirstStartComplete` / `AllAppReadySyncHook` | 应用层启动完成 |
| `BatteryDetected` / `BatteryLevel` / `BatteryErrorBody` | 电池 |

如果 QEMU 里这些设备是占位实现、事件从未投递，相机就会停在"不可拍"状态——
这可能比"没有快门通道"更早地拦住拍摄。

**并行验证方法（不需要新固件）**：本补丁的 RING 记录每个钩点的 `{slot, arg0, arg1, lr}`。
按不同 pin 后 dump `0xC5EFF100` 起的 48 条描述符，比较 `lr` 的分布，
就能看出每个按键走的是哪条代码路径——这是现成的、无需改包的路由指纹。

## 13. 1s JPEG 卡环形 · 待真机探针的最终规格

在 QEMU 能触发拍摄之前，这一档位定为**待真机探针**。规格冻结如下：

### 13.1 目标
- 档位：**1s JPEG FX @14fps**（14 帧，50–85 MB）
- 介质：卡上环形文件（避免 37.25 MB 应用堆限制）
- 行为：待机时连续写环；全按时把最近 1 s 转正；环形深度 14 帧

### 13.2 仍缺的两条实证
1. **JPEG 产出点**：哪段代码把编码结果交出来（本版探针若命中 `jpeg_slot` 即得解）
2. **写卡调用点**：文件服务写入函数的入口

### 13.3 真机探针步骤
1. 刷入 `Z6_2_0170_preshoot_probe.bin`，拍一张照片；
2. `0xC5EFF020` 若 `jpeg_hits > 0` → `[0xC5EFF024]` 给出 slot，`[0xC5EFF028]` 给出缓冲区地址；
   直接对该地址设写监视，抓产出点 PC，即第 9 个钩点；
3. 若 `jpeg_hits == 0` → 说明码流不在 `0xC2C00000..0xC5F00000` 或不经 mas；
   需扩大探针窗口（改 `hookgen.PROBE_LO/PROBE_HI`，需先确认该段可读）或在 sla 侧重复同一套探针；
4. 写卡点：对 `1s JPEG` 而言，最省事的实现是**不复制像素**——在 JPEG 交给文件服务的那一刻
   直接写卡环形，只保留文件句柄与索引。这要求第 2 步先成功。

### 13.4 判据
- 探针包冷启动：PASS、magic 存在、无 Data Abort（已达成）
- 真机拍摄一次后 `jpeg_hits > 0` 且 `jpeg_slot` 稳定指向同一槽
- 拿到产出点后，新增钩点的 A/B 双执行必须仍等同原厂

## 14. ExMCU 接收路径：查到的与查不到的

### 14.1 查到的

| 名字 | 位置 | 用途 |
|---|---|---|
| `ExMcuEvt` / `ExMcuFL` 注册 | `f01e624e` / `f01e6264`（引用点 `f01e6258` / `f01e6286`） | 冷启动时把事件/标志对象注册进对象系统；随后经 `[obj+0x64]`、`[obj]` 的函数指针回调 |
| `ExMcuCommDebug` 上下文 | `f01eba8c` 起 | 通信调试对象的初始化序列（`bl 0x103727a4` / `0x1037281c` / `0x1036ac1a`） |
| 物理层驱动 | `I2cDriver.c` `f20afabc`、`BI2cWrapperServic` `f242cdc3`、`AUartDriver.c` `f20b014f`、`UartDriver.c` `f20b05ac` | 候选通道 |
| 通道名表 | `SIO_0_RX/TX`…`SIO_2`、`UART_0_RX/TX`…`UART_2`、`I2C_0..3`、`SIF_0..7`（`f076be16`–`f076c1f6`） | 外设通道枚举，但**不含 ExMCU 专用通道** |

### 14.2 查不到的

- **`ExMcuService` / `ExMcuCommandServ` / `BExMcuProtocolSer` 三个 tag 在 mas 里零 dword 引用、
  零可定位 adr 引用**：它们只被"按名字注册/查找"的对象系统使用，代码中没有直接的字符串地址。
- 因此**无法给出 ExMCU 命令的接收函数地址，也无法给出 transport（I2C 地址 / 波特率 / 帧格式）**。
  事件回调是通过运行期填入的函数指针（`blx r1`）分发的，静态看不到目标。

### 14.3 可做的 trace（判断 ExMCU 服务是否活着）

| 地址 | 说明 |
|---|---|
| `0x1020624e` | `ExMcuEvt` 对象注册；冷启动命中 = ExMCU 服务初始化过 |
| `0x10206264` | 紧随其后的回调分发（`ldr r1,[r0,#0x64]; blx r1`） |
| `0x1020ba8c` | ExMCU 通信调试上下文初始化 |

若这三处冷启动都不命中 → mas 侧 ExMCU 服务根本没起来（比"没有快门通道"更根本）。
若命中但按键后无增量 → 服务在，但物理层没有事件进来，需要 QEMU 补 ExMCU 设备模型。

## 15. 真机采集的最小数据清单（1s JPEG 卡环形定稿版）

在仿真机不模拟 ExMCU、无法触发拍摄的前提下，以下六项是真机侧的全部必需输入。
每项都给出采集方式与用途，**没有一项需要刷非官方固件之外的额外改动**。

| # | 数据 | 采集方式 | 用途 |
|---|---|---|---|
| 1 | 拍摄一张后的 `STATUS`（`0xC5EFF000`，64 B） | 刷探针包，拍一张，读内存 | `jpeg_hits/jpeg_slot/jpeg_addr` 直接给出数据面切点 |
| 2 | 全 RAM 中 `FF D8 FF` 的地址范围 | 拍后 dump `0xC0000000..0xC6000000`（或整段 DDR），离线搜 | 若探针未命中，用它定位 JPEG 缓冲区所在区间 |
| 3 | 该区间的**首个写入者 PC** | 对第 2 步找到的地址设写监视（硬件断点/调试器） | 即"JPEG 产出点"，做成第 9 个钩点 |
| 4 | 连续拍 14 张时 heap 的增量 | 每拍一张 dump 一次，比较非零页 | 确定帧池容量与可保留帧数 |
| 5 | JPEG 文件实际大小分布 | 卡上的文件尺寸（≥100 张样本） | 决定环形深度：14 帧 × 实际均值 |
| 6 | 写卡调用的入口 | 对文件服务设 trace（或抓 `fwrite` 类封装的调用栈） | 卡环形的写入点 |

**判定顺序**：
- 第 1 项命中 → 直接进第 3 项，跳过第 2 项；
- 第 1 项未命中、第 2 项找到 → 说明码流不在探针窗口或不经 mas 钩点，用第 3 项反推调用者；
- 第 1、2 项都空 → 说明 JPEG 由 sla 生成且 mas 只持句柄；此时需在 sla 侧重复同一套探针
  （钩子层可直接复用：`MAS_BASE`→`SLA_BASE`，重新选站点）。

**已冻结的实现参数**（与 §13 一致）：1s / 14 帧 / FX / 卡上环形 / 全按转正 / 环形深度 14。
若第 5 项显示单帧均值 > 6 MB，则 14 帧超过 84 MB，需要把档位降为 DX 或把深度改为按字节预算动态计算。

## 16. 仿真机上已排除的触发假设（实测记录）

> 说明：§16 的 G14/G15 假设**不是本 lab 提出的** —— 本 lab 给出的候选只有 ExMCU 通道（§14）
> 与 §11 的五个快门链函数地址，从未给出 GPIO 引脚号。此节按主线实测结果如实记录，
> 避免后续误用该假设。

### 16.1 pad 注入实测

测试平台：`nikon-z72-controller-config`（`/machine/unattached/device[22]`），128 条 pad 线，
索引 = `port*16 + pin`。

| 注入 | STATUS count | 快门链 trace | heap `FF D8 FF` | 结论 |
|---|---|---|---|---|
| pad G14（线 110） | 245 → 245（不变） | 0 命中 | 0 | 不是快门 |
| pad G15（线 111） | 245 → 273（+28，与普通 UI 键同量级） | 0 命中 | 0 | **是普通 UI 键**，不是快门 |
| G14 长按 + G15 同时按下 | 无新增 | 0 命中 | 0 | 组合也不是 |

对照：按键扩展器 24 pin 中能出事件的 8 个（1/2/11/14/17/19/21/23）也是 +28…+34、快门链 0。

### 16.2 若真机确认 G14/G15 是外接快门

那么 QEMU 侧必须补**该 pad 的极性与时序模型**：半按/全按的电气时序、去抖窗口、
以及"按下沿 → 事件"的映射关系。当前模型下该 pad 的注入只会被当作普通 UI 键，
因此既有实测不能证伪"G14/G15 是真机外接快门"，只能证伪"本模型里 G14/G15 会走快门链"。

### 16.3 128 线全扫（已完成）

pad 阵列 128 条线全部注入完毕，跳过模型已占用的 9 条（37/61/73/101/102/103/30/67/60）。

| 结果 | 数值 |
|---|---|
| 产生 `count` 增量的线 | **46 条**，增量 +1…+41，全部为 UI 级事件 |
| 扫描期间唯一一次 trace 增量 | 线 38（P2.6）+326，**仅出现一次** |
| 线 38 聚焦复测 | 3 次脉冲 + 4 s 长按，trace **恒为 0**，`jpeg_hits=0`，heap 无 JPEG SOI |
| 线 38 判定 | **扫描窗口内的延迟 TB 日志伪影**，不是快门链 |

结论：**pad 阵列里没有快门输入，GPIO 路径排除。**

### 16.4 LV / CAPTG 证据

仿真机上 LV（取景 HUD）**默认开启**，而 CAPTG 在其之前就已存在 —— 因此 **CAPTG 不是 LV 创建的**。
这条证据否定了"CAPTG 由 LV 会话建立"的推断；"CAPTG 由谁创建"仍需在真机或完整相机模型上另找，
它与"连拍会话"的前置条件有关，但不影响 §13/§15 已冻结的 1s JPEG 卡环形规格。

### 16.5 收口结论（最终）

三条相互独立的否定证据合在一起，仿真机侧**不存在任何可用的拍摄触发路径**：

1. **GPIO**：128 条 pad 全扫，46 条只产生 UI 事件，无一条走快门链（§16.3）；
2. **ExMCU**：`ExMcuService/CommandServ/BExMcuProtocolSer` 只被按名注册，回调经运行期函数指针
   分发，transport 与协议静态不可得；QEMU 无该设备模型（§14）；
3. **LV / Release mode 不可达**：LV 默认开启、CAPTG 先于 LV 存在，Release 相关状态机无法进入。

因此：

- **预拍摄功能与 1s JPEG 卡环形方案（§13/§15）只等真机探针数据**；
- **仿真机侧不再尝试任何触发路径**，其结论固定为：**启动 PASS + 无拍摄链**；
- 本 lab 不再为仿真机产出新包。包保持 `Z6_2_0170_preshoot_probe.bin` 不变
  （49,002,888 B，K8 重签，包 CRC `cf9a`，冷启动 314/299 PASS）。
- 恢复工作的唯一入口是真机 §15 的第 1 项：拍一张后读 `0xC5EFF000` 的 64 B 状态块。
