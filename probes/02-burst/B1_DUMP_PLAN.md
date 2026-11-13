# B1 · NP3 分页导出方案（连拍线，按真实 NP3 格式修正）

日期：2026-10-30　通道：保存 Picture Control（NP3 tone curve 块）　前提：探针先把数据记到 DRAM，用户再分页保存导出

> 本文件只出方案与文档，不写固件、不刷机。导出 hook 由预拍摄会话实现；本文件定义数据布局、页协议、场景清单和限制。
>
> **真实 NP3 格式（本地样本 `work/color/tmpout/card/PICCON219.NP3` + `work/color/z6ii_np3.py` 实测）**
> - 文件头 `NCP\0` 在偏移 0；TLV `{BE32 tag, BE32 len}`。
> - tone curve tag `0x0002`，len `0x242`；payload 的 `+0x00..0x3F` 是 0x40 字节块头，`+0x40` 起是 **257 个 BE16**（514 B 数据区）。
> - 曲线值域 **0..0x7FFF**；样本曲线单调，值 1285..29298。
> - 0x40 块头的 head/meta 字段必须保持合法；只有其中保留的零区可以借用。
>
> **安全边界**：B1 的唯一落盘是相机正常的 Picture Control 保存（FAT 上的 NP3 文件），不是裸扇区写；不写 NOR、分区表、引导扇区、固件。探针只读运行期状态并写 DRAM。

## 0. 结论与硬限制

- **B1 不是流式 trace**：探针先把一个场景的数据写进 DRAM 固定缓冲区，用户再按页保存 Picture Control；不能边拍边传。
- **真实曲线块**：tag `0x0002`、len `0x242` = 0x40 块头 + **514 B 数据区（257×2）**，值域 0..0x7FFF。
- **页头放在 0x40 块头的保留区**（`payload+0x18..0x27`），不占 514 B 数据区：
  - **raw 模式**：514 B 数据/次；
  - **单调模式**：222 B 数据/次（0..0x7FFF 下的 7-bit carrier）。
- 514 B 数据区装不下 514 B 的严格单调曲线：257 个 0..0x7FFF 的单调值容量约 2,170 bit ≈ 271 B；本方案用简单 carrier 取 **222 B/次**。要 514 B/次只能用 raw；若相机校验单调，必须接受“3 次保存换 1 个 514 B 逻辑页”（222+222+70）。
- 下文的保存次数按 **net 数据**（raw 514 B/次、单调 222 B/次）计算。

## 1. 需要导出的运行期数据

### 1.1 完整包（每个场景）

| 段 | 大小 | 内容 | 备注 |
|---|---:|---|---|
| Manifest | 256 B | 场景 ID、模式设置、段表、丢弃计数 | 放在 payload 流起始 |
| SIF log | 2,048 B | 32 事件 × 64 B | `ts, ch, flags, r0..r3, r1[0x00..0x1F]`，环形 |
| Mode records | 6,912 B | 6 × 0x480 | Still/StillSilent/LvStill/LvStillSilent/LvMovieRec/LvMovie |
| Registry | 848 B | 0x350 | 53 × 0x10 对象注册表 |
| Frame ring | 256 B | 64 × u32 | 最近 64 个帧周期/时间戳 |
| MMIO counters | 256 B | 16 × 16 B | `addr, writes, last_value, last_pc`；**不是全量 trace** |
| **合计** | **10,576 B** | | |

### 1.2 精简包（每个场景，推荐）

| 段 | 大小 | 内容 |
|---|---:|---|
| Manifest | 256 B | 同上 |
| SIF log | 1,024 B | 16 事件 × 64 B |
| Mode records | 2,304 B | 只导 Still + LvStill（2 × 0x480） |
| Registry | 848 B | 同上 |
| Frame ring | 256 B | 同上 |
| MMIO counters | 256 B | 同上 |
| **合计** | **4,944 B** | |

### 1.3 一次性全量模式记录

6 × 0x480 = **6,912 B**，整个会话导一次即可（场景 ID 0），不必每个场景都导。

### 1.4 保存次数（按 514 B 数据区重算）

| 包 | 数据 | raw 514 B/次 | 单调 222 B/次 |
|---|---:|---:|---:|
| 完整包/场景 | 10,576 B | **21 次** | **48 次** |
| 精简包/场景 | 4,944 B | **10 次** | **23 次** |
| 全量模式记录（一次） | 6,912 B | **14 次** | **32 次** |

- raw 的 514 B 是**净载**（页头在 0x40 块头保留区，不占数据区）。
- 如果块头保留区不能被改写、只能用数据区开头放页头，raw 净载降到 498 B，次数会 +1；单调 fallback 不推荐。

### 1.5 不建议导出的

- **MMIO 全量 trace**：30fps 下每帧可能上千条写，514 B/次要几百次保存，不可操作。**放弃全量，改成 16 个事件计数器**；如果计数器也不够，P5 直接标“真机不可导”。
- SIF 全量日志：只保留 16/32 条环形样本；溢出时在 manifest 记 `dropped_sif`。

## 2. Z6PD 页协议（与预拍摄会话共用头/编码）

### 2.1 16 B 页头

| 偏移 | 字段 | 类型 | 说明 |
|---:|---|---|---|
| 0x00 | magic | 4 B | ASCII `Z6PD` |
| 0x04 | version | u8 | `1` |
| 0x05 | flags | u8 | bit0：0=raw，1=单调；bit1：最后一页；bit2：1=页头在 0x40 块头保留区，0=页头在数据区开头；bit3..7 保留 |
| 0x06 | scenario | u16 | 场景 ID（1–9；0=一次性全量） |
| 0x08 | page | u16 | 本次保存的页号，0..total-1 |
| 0x0A | total | u16 | 本场景总保存次数 |
| 0x0C | payload_len | u16 | raw=514，单调=222 |
| 0x0E | crc16 | u16 | CRC16-CCITT-FALSE，覆盖 header[0x00:0x0E] + payload |

- CRC 多项式 `0x1021`、init `0xFFFF`；如果预拍摄的 Z6PD v1 用 init `0x0000`，只改 init，字段顺序不变。
- `page`/`total` 是**保存次数**，不是 514 B 逻辑页号；解码器按 `page` 排序后拼接 payload。
- `flags.bit2` 是给预拍摄会话共用解码器的关键：连拍线=1（块头保留区），预拍摄若放数据区=0；解码器按位取页头。

### 2.2 页头放哪里

**主方案：0x40 块头的保留区。**

- 导出 hook 先复制当前 Picture Control 的 tag `0x0002` payload 的 0x40 块头；**head 和 meta 的已用前缀（len+points，样本 13 B）原样保留**（样本 head=`493000ff0ae40100`，meta=`0600002e1662559ca4e4efffff`，因 PC 而异，不能重建固定头；已用前缀 < 0x18）。
- 把 Z6PD 页头写到 `payload[0x18:0x28]`（块头内、保留零区；样本该区全 0）。
- 曲线数据区 `payload[0x40:0x242]`（514 B / 257 BE16）全部用于 payload；raw 净载 514 B。
- **257 vs 256 的边界（2026-10-30 已定）**：`0x103DC792`（文件 `0x3BC792`）的循环只 rev16 前 256 个字；第 257 个字（payload+0x240）原样搬进文件。相机内存里这 2 字节本来就按 BE 保存（载入时也不转），所以文件始终有 257 个大端点。导出 hook 覆写前 512 B 即可，第 257 点保持原值；若要 514 B 净载，可把最后 2 字节按小端写（rev16 不翻转它），解码端单独按小端读。
- 解码器区分：
  - 读 tag `0x0002`、len `0x242` 的 payload；
  - 若 `payload[0x18:0x1C] == b"Z6PD"` 且 CRC 通过 → 我们的页；
  - 否则按正常 PC 曲线处理（正常 PC 的 0x18..0x27 为 0）。
- 风险：如果相机校验块头保留区必须全 0，页会被拒。**先用 1 页测试**；被拒则用 fallback。

**fallback（块头保留区不可写）：页头放数据区开头。**

- 页头写 `payload[0x40:0x50]`；raw 净载 498 B（514-16）。
- 单调 fallback 不推荐（页头会破坏曲线单调，需要另设计数器编码）；只做 raw。
- 解码器按 `flags.bit2=0` 从数据区开头读页头。

### 2.3 raw 模式（优先尝试）

- 514 B 数据区 = 257 个 BE16，直接写 payload。
- 优点：容量最大，514 B/次；页头在块头，不占数据。
- 风险：相机可能校验曲线端点/单调；raw 的任意 16-bit 值可能被拒。**先用 1 页测试**；被拒就切单调模式。

### 2.4 单调模式（曲线校验的保险，0..0x7FFF）

把 **222 B payload** 编码成一条严格递增的 257 点曲线；页头在 0x40 块头，不占数据区：

```
v[0] = 0
v[i] = i*128 + d[i-1]        i = 1..254, d[x] ∈ [0,127]
v[255] = 32766
v[256] = 32767
```

- 254 个 7-bit 数字 = 1,778 bit；222 B = 1,776 bit，剩 2 bit 填充。
- 编码器：把 222 B 当 1,776-bit 大整数，按 254 个 7-bit 从高到低切；最高 2 位自然为 0（等价于左补 2 个 0 bit）。解码器把 254 个数字拼回 1,778-bit 后取低 1,776 bit，或直接当 1,776-bit 整数。
- 严格递增证明：
  - `v[1]-v[0] = 128 + d[0] ≥ 128`
  - `v[i+1]-v[i] = 128 + d[i]-d[i-1] ≥ 1`（d 最大 127）
  - `v[255]-v[254] = 32766 - (254*128 + d[253]) = 254 - d[253] ≥ 127`
  - `v[256]-v[255] = 1`
- 所有值 ≤32767；v[254] ≤32639，v[255]=32766，v[256]=32767。
- 解码：`d[i-1] = v[i] - i*128`（i=1..254），按 7-bit 数字解包成 222 B。
- 曲线是 0..0x7FFF 的线性 ramp 加 7-bit 扰动；样本曲线 1285..29298 只用了中段，0/32767 在值域内。如果相机额外要求“首点≥1285、末点≤29298”，需要整体平移/缩放，容量会下降（约 180 B/次），实测后再定。
- 小例子：`d[i]=i&0x7F` → `v[1]=128, v[2]=256+1=257, …`；解码回来是同样的 d。
- **必须先测正常 PC 曲线的 endpoints**：如果相机不接受 v[0]=0 或 v[256]=32767，按样本的 1285/29298 平移/缩放，并重算 7-bit 数字个数和容量。

### 2.5 payload 流与 manifest

- 解码器把所有页的 payload（raw 514 B/页，单调 222 B/页）按 `page` 拼成一个连续流；流的**前 256 B 是 manifest**。
- Manifest（256 B）：
  - magic `Z6PD`（4 B）
  - version u8、scenario u8、flags u8、section_count u8
  - total_bytes u32、dropped_sif u16、dropped_mmio u16
  - scenario_name[16]、settings[16]
  - section entries：每项 `type u16, flags u16, offset u32, length u32`（12 B），最多 8 项
- `settings[16]` 建议：`[area, size, bit_depth, release, media, fps, 0…]`
  - area：0=FX，1=DX，2=1:1，3=16:9
  - size：0=L，1=M，2=S
  - bit_depth：12/14
  - release：0=S，1=CL，2=CH，3=CH extended
  - media：0=still，1=movie；fps：5/14/30/60
- Section 类型：1=SIF log，2=mode records，3=registry，4=frame ring，5=MMIO counters，6=task/其他。
- `offset` 从 payload 流起点算；manifest 之后是各段数据。

### 2.6 场景标记与分页触发

三种方式，优先 A：

- **A. 自动（推荐）**：探针读当前模式状态（FX/DX、L/M/S、12/14-bit、release mode、movie/still），算 `scenario_id` 并写 manifest。
- **B. PC 名标签**：用户选一个名为 `B1S1`..`B1S9` 的 Picture Control；探针读当前 PC 名，取数字作 `scenario_id`。
- **C. 固定顺序**：用户按 1..9 的顺序导出；探针每 N 次保存后 `scenario_id++`；解码器按顺序校验 manifest 的设置。

分页/重复保存：

- 探针维护 `page` 和 `page_ready`。每保存一次，导出当前页并 `page++`；最后一页后 `page_ready=0`。
- 如果用户在 `page_ready=0` 时再保存，探针**重复最后一页**（同页号/同数据），解码器按 `(scenario,page)` 去重。
- 开始新场景：模式变化或选择新的 PC 名；探针重置 `page=0`、`scenario++`。若都无法检测，用固定顺序 C。

### 2.7 文件命名

- 相机保存的 NP3 文件是 `PICCONxxx.NP3`。v1.70 原生导出为 **834 B（0x342）**：tag `0x0002` 头在 `+0xF4`、body 在 `+0xFC`、表在 `+0x13C`；`PICCON219.NP3` 的 1234 B 是带 0x1F00/0x2000/0x10100 扩展块的变体。导出 hook 负责写 tag `0x0002` 的 0x40 块头和表区。
- 用户复制后重命名：`B1_S<场景>_P<页>of<总数>.NP3`，例如 `B1_S03_P00of20.NP3`。
- 文件头是权威；文件名只给人看。解码器按页头排序，不依赖文件名。

### 2.8 解码器伪代码

```python
def be16(x): return [int.from_bytes(x[i:i+2], "big") for i in range(0, 514, 2)]
def u16(x, o): return int.from_bytes(x[o:o+2], "big")

def unpack_7bit(digits, nbytes=222):
    bits = 0
    for d in digits:
        assert 0 <= d <= 127
        bits = (bits << 7) | d
    assert bits < (1 << (8*nbytes))       # 编码器左补 2 bit 0
    return bits.to_bytes(nbytes, "big")

def decode_curve(payload_0x242):
    block = payload_0x242[:0x40]
    data = payload_0x242[0x40:0x242]      # 514 B
    if block[0x18:0x1C] == b"Z6PD":
        hdr = block[0x18:0x28]
    else:
        hdr = data[:16]                    # fallback
        data = data[16:]
    monotonic = hdr[5] & 1
    if monotonic:
        vals = be16(data)
        digits = [vals[i] - i*128 for i in range(1, 255)]
        payload = unpack_7bit(digits, 222)
    else:
        payload = data[:u16(hdr, 0x0C)]
    assert crc16(hdr[:14] + payload) == u16(hdr, 0x0E)
    return hdr, payload

def decode(files):
    pages = {}
    for f in files:
        payload_0x242 = extract_tag_2(f)   # 预拍摄 hook 已知的 NP3 内偏移
        hdr, payload = decode_curve(payload_0x242)
        scenario = u16(hdr, 0x06)
        page = u16(hdr, 0x08)
        pages[(scenario, page)] = (hdr, payload)
    for scenario in scenarios:
        total = u16(pages[(scenario, 0)][0], 0x0A)
        stream = b"".join(pages[(scenario, p)][1] for p in range(total))
        manifest = parse_manifest(stream[:256])
        for sec in manifest.sections:
            yield sec.type, stream[sec.offset:sec.offset + sec.length]
```

- 缺页/CRC 失败 → 该场景重导；不要用“按文件大小补齐”猜。
- `total` 必须等于实际页数；多出的重复页按 `(scenario,page)` 去重。

## 3. 每个场景的最小操作清单

通用菜单（Photo Shooting Menu 第 1 页）：

- **Image quality**：`NEF (RAW)`（或 `NEF (RAW)+JPEG`，但 RAW 更干净）
- **Image size**：L / M / S
- **Choose image area**：FX / DX
- **NEF (RAW) recording** → `NEF (RAW) compression = Lossless compressed`，`NEF (RAW) bit depth = 12-bit / 14-bit`
- **Release mode**：c/E 按钮 + 主指令拨盘（S / CL / CH / CH extended），不在菜单
- **Movie 模式**：拍照/视频物理拨杆；真机操作

| ID | 场景 | 设置 | 拍摄 | 精简包 raw/单调 | 完整包 raw/单调 |
|---:|---|---|---|---:|---:|
| 1 | 单张 FX 12-bit | Single，FX，L，RAW 12-bit | 1 张 | 10 / 23 | 21 / 48 |
| 2 | CH 5fps FX | CL，FX，L，RAW 12-bit | 按住 1 s | 10 / 23 | 21 / 48 |
| 3 | CH extended 14fps FX 12-bit | CH extended，FX，L，RAW 12-bit | 按住 1 s | 10 / 23 | 21 / 48 |
| 4 | CH extended 14fps FX 14-bit | 同上，14-bit | 按住 1 s | 10 / 23 | 21 / 48 |
| 5 | 视频 4K30 DX | Movie，DX，4K30 | 录 2 s | 10 / 23 | 21 / 48 |
| 6 | 视频 4K60 DX | Movie，DX，4K60 | 录 2 s | 10 / 23 | 21 / 48 |
| 7 | 静止 DX L 14fps 12-bit | CH extended，DX，L，RAW 12-bit | 按住 1 s | 10 / 23 | 21 / 48 |
| 8 | 静止 DX M 14fps 12-bit | CH extended，DX，M，RAW 12-bit | 按住 1 s | 10 / 23 | 21 / 48 |
| 9 | 静止 DX S 14fps 12-bit | CH extended，DX，S，RAW 12-bit | 按住 1 s | 10 / 23 | 21 / 48 |

- **一次性全量模式记录**（场景 0）：raw 14 次 / 单调 32 次，整个会话一次。
- 如果每个场景都要全量 6 条模式记录，用“完整包”列 21/48 次。
- 推荐：场景 1/2/5/6/8/9 用精简包，场景 3/4/7 用完整包；总量可控。
- 8/9 是 DX M 和 DX S 分开；如果只想按“DX M-S”一个场景做，可合并为 ID 8，导两次或只导 M；分开的好处是 registry/mode record 不会混。

每个场景的步骤：

1. 设好菜单/释放模式，确认画面区域和尺寸。
2. 拍摄（单张/1 s 连拍/2 s 视频）。
3. 保持相机开机，进入保存 Picture Control；**连续保存 N 次**（N 见上表）。
4. 每次保存后把 NP3 文件复制出卡并重命名；不要在保存过程中关机。
5. 检查 manifest 的 `scenario/settings` 是否与当前设置一致；不一致就重导该场景。

## 4. 明确限制

- **B1 不是流式**：探针先把数据写 DRAM；DRAM 缓冲约 12 KB（完整包）/ 8 KB（精简包）。SIF 溢出时只保留环形样本，manifest 记 `dropped_sif`。
- **MMIO 全量 trace 放弃**：改成 16 个计数器（`addr, writes, last_value, last_pc`）；如果 16 个不够，P5 标“真机不可导”，不要试图用 B1 传全量。
- **保存次数多**：精简包 9 场景 90/207 次，加一次性模式记录 14/32 = **104/239 次**；完整包 189/432 次。优先导出场景 3/4/7/8，其他按需。
- **曲线校验风险**：raw 先试 1 页；被拒改单调。单调模式 222 B/次，不能达到 514 B/次；这是 0..0x7FFF 的容量限制，不是实现问题。
- **块头风险**：页头借用 0x40 块头的保留区；head/meta[0:7] 必须原样复制。若相机校验保留区必须全 0，用 fallback（页头放数据区开头，raw 498 B）。
- **NP3 文件 834 B（v1.70 原生）或 1234 B（扩展变体）**：0x242 曲线块只是其中一段；导出 hook 负责写块头和表区，用户只负责保存/复制文件。
- **不写固件/不刷机**：本方案只定义 DRAM 数据和 NP3 曲线；导出 hook 由预拍摄会话实现。

## 5. 与 Z6PD 的兼容性

- 共用：16 B 头（magic/version/flags/scenario/page/total/payload_len/crc16）、CRC16、raw/单调编码、manifest TLV。
- 新增 `flags.bit2` 表示页头位置：
  - 1 = 页头在 tag `0x0002` 的 0x40 块头保留区（连拍线主方案）；
  - 0 = 页头在曲线数据区开头（预拍摄会话/fallback）。
- 允许不同：section 类型表（预拍摄会话可加自己的 type）；payload 内容。
- 若预拍摄的 Z6PD v1 字段顺序与本文不同，以预拍摄为准；本文的 **0..0x7FFF 单调编码公式**和 **manifest TLV** 可直接复用。
- 解码器流程：按 `scenario,page` 排序 → 按 `flags.bit2` 取页头 → 校验 CRC → 拼接 payload → 读 manifest → 按 section 表切段 → 解析。

## 6. 一页测试建议（先做这个）

正式导 9 个场景前，先做一次 **单页测试**：

1. 用 hook 读一份正常 PC 的 tag `0x0002` 块，记录：文件偏移、0x40 块头、257 点曲线的 min/max/endpoints、是否单调。确认 0x18..0x27 是保留零区。
2. 复制该块头，把 manifest page 0 的 Z6PD 头写到 `payload[0x18:0x28]`；raw 模式填 514 B。
3. 保存一次 Picture Control；确认相机接受、NP3 文件 1234 B、tag 0x2 len 0x242。
4. 用解码器读回：magic/version/scenario/page/total/crc 正确，manifest 可解析。
5. 若 raw 被拒（曲线校验），切单调模式，重复 1–3；确认 `v[0]=0`、`v[256]=32767`、严格递增，222 B payload 可解。
6. 单页测试通过后，再按第 3 节批量导出。

单页测试只需要 1 次保存；失败不会写相机/NOR，只是 NP3 文件不被接受。
