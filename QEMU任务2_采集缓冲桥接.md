# 任务 2：sensor 数据接回固件采集缓冲

日期：2026-11-12
状态：**桥已建成，像素已就位；固件尚未消费 —— 缺口定位到 ISP 中断语义**

---

## 1. 采集缓冲地址：三重证据锁定

### 证据链

| 来源 | 内容 |
|---|---|
| 固件 scatter 表 | `0x10034070` 一行 = `{src=0x123a1c14, dst=0x80800000, len=0x2313880, tag=0x10020088}` |
| `nikon-z62-profile.h` | `{0x80800000, 0x02313880, "z62-physical-capture-buffer-lab"}` — 逐字节吻合 |
| 容量推断 | `0x2313880` = 36,782,080 B；6048×4024×1.5 = 36,505,728 B（12-bit packed 24.3MP Bayer）→ 量级匹配 |

### 完整 scatter 表（mas，`0x10034040` 起，16 字节一行）

| 行 | dst | len | 含义 |
|---|---|---|---|
| `0x10034040` | `0x04008fe0` | `0x20` | OCM boot stack |
| `0x10034050` | `0x80000000` | `0x10000` | physical-local |
| `0x10034060` | `0x80400000` | `0x340000` | work buffer (3.25 MB) |
| **`0x10034070`** | **`0x80800000`** | **`0x2313880`** | **capture buffer (35 MB)** |
| `0x10034080` | `0x82c00000` | `0x82bc400` | processing buffer |
| `0x10034090` | `0x8af00000` | `0x2044400` | image buffer |
| `0x100340a0` | `0x8d000000` | `0x31d17000` | frame buffer |
| `0x100340b0` | `0xbf700000` | `0x824a00` | frame tail |
| `0x100340c0` | `0xc18aedc0` | `0x1298a90` | master heap |
| `0x100340d0` | `0xc617c478` | `0x1d4bc48` | peer/startup |
| `0x100340f0` | `0xccb00000` | `0xda0000` | secondary pool |
| `0x10034100` | `0xcd900000` | `0x10e0000` | tertiary pool |
| `0x10034110` | `0xcea00000` | `0x63cb400` | processing pool |
| `0x10034120` | `0xd4e00000` | `0x883b000` | image pool |
| `0x10034130` | `0xdd700000` | `0x228c2800` | frame pool |

> 注意：`profile.h` 明确写 *"CPU virtual aliases are supplied by the original firmware's MMU"* ——
> 这些是**物理**地址，固件通过自己的 MMU 用虚拟地址访问同一批物理页。

---

## 2. 已实现的桥

新增 `z62_sensor_sim_fill_capture()`（`nikon-z62-sensor-sim.c`），并在抓拍链两处接入：

```
capseq 快门/完成
  ├─ laboratory-shutter-hardware 路径：shutter_tick → EXPOSE → shutter_sensor_capture()
  └─ laboratory-capseq-actuator 路径：capseq_tick → capseq_complete()
        ↓ 两条都调用
  z62_sensor_sim_fill_capture(sensor_sim, 0x80800000, exposure_ns)
        ↓
  生成 RGGB Bayer（复用 sensor_scene_pixel，rgb*64 → 0..16320）
  16-bit 小端，1920×1080 = 4,147,200 字节
  address_space_write 到物理 0x80800000
```

### 实测输出

```
Z62_CAPTURE_FILL address=80800000 1920x1080 bytes=4147200 exposure_ns=200000 iso=100
```

### 内存回读（monitor `xp/16xw 0x80800000`）

```
80800000: 0x3a803a80 0x3a803b40 0x3ac03b40 0x3b003a80
80800010: 0x3ac03a40 0x3a403a80 0x3ac03b40 0x3b003a40
```

`0x3a80` = 14976 = 234×64、`0x3b40` = 15168 = 237×64 —— 正是 scene 像素乘 64 的值。
50s 与 72s 两次 dump 内容一致，未被固件清零。

---

## 3. 固件侧：拍照路径确实是活的

开启 `laboratory-capseq-trace` 后抓到完整时序：

```
request-rise  address=04282018 value=01000100 pc=1038817a   ← 固件写拍照请求
complete      address=04282014 value=00000080                ← 快门完成
gpio-config   address=0426f010 value=00000001 pc=103883f2   ← 固件 9μs 后响应，驱动快门/闪光 GPIO
request-rise  address=04282018 value=01000100                ← 固件发起第二次
```

`0x04282018` 的写入来自通用位操作函数（`0x1038815b..0x1038817d`）：
置 `bit N` 与 `bit N+16`（N=8），即"置请求位 + 置其使能位"。

`pc=0x1038817a` 就是那个 `str r2,[r4]`。

### 一个踩过的坑

`capseq-delay-ns` 必须**小于请求脉冲宽度（实测约 1.34 ms）**。
第一次设 5 ms，tick 时请求已落下 → `capseq_request=false` → 直接取消，永远不 complete。
改 0.2 ms 后完成，固件立刻响应。

---

## 4. 固件为何仍不写卡：缺口定位

DCIM 目录始终为空，SD 卡只有 FAT 表被改动（目录扫描/时间戳级操作）。

读 QEMU 侧源码后缺口很清楚 —— 图像链被拆成**四个互不相连的"实验室完成桩"**：

| 桩 | 开关 | 作用 | 源码注释原文 |
|---|---|---|---|
| sensor 帧结束 | `laboratory-frame-completion` | 只计数 `frame_completions` | — |
| ISP 输入引擎 | `laboratory-isp-input-cycle` | 置 `0x20068210 bit0` → IRQ94 | *"not a pixel-transfer completion source"* |
| 快门序列 | `laboratory-capseq-actuator` | 置 `0x0426f008 bit0` | *"not recovered silicon timing"* |
| sensor 配置 | `laboratory-mode/init-completion` | 模式/初始化应答 | — |

`nikon-z62-isp-input-core.h` 更直接：

> *"No DMA, pixels, metering statistics or application-state success is produced here."*

而 `frame_sync_tick` 里的注释划出了关键分界：

> *"Native bank0 bit0 / IRQ94, **not sensor IRQ89 or pixel DMA**."*

**所以缺的一环是**：把"采集缓冲里已有像素"这件事，通过 **sensor 中断（IRQ89）/ 帧就绪状态位**告诉固件。
目前没有任何模型产生这个信号，固件因此停在"等像素"。

### 下一步（已量化）

1. 反推 IRQ89（sensor）在固件里的 handler，确认它读哪些状态位、从哪个地址取像素；
2. 在 `capseq_complete` 之后追加：置该状态位 + 拉起 IRQ89，并把 `0x80800000` 的有效行数/尺寸写进固件期望的描述符；
3. 观察 CaptureService 是否进入编码 → DCIM 落文件。

---

## 5. 复现命令

```bash
python work/qemu_build/board_probe.py --main-screen \
  --sd D:/z6ii_fw_lab/.local/sdcard.raw --sd-write \
  --at 50,72 --menu-start 42 --menu-every 0 \
  --extra "nikon-z72-boot-config.laboratory-capseq-trace=on,\
nikon-z72-boot-config.laboratory-capseq-actuator=on,\
nikon-z72-boot-config.capseq-delay-ns=200000"
```

日志：`grep -aE "Z62_CAPSEQ|Z62_CAPTURE_FILL" .local/z62-smoke/board.txt`
