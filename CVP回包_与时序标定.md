# CVP 回包能力 + 时序标定机制

日期：2026-11-12
配套：`QEMU任务123_本轮交付.md`（上一轮）、`QEMU第二块Flash_写入踪迹.md`

---

## 一、CVP：从"只会丢弃命令"变成"能回答任何命令"

### 加的是什么，依据是什么

之前 `cvp_write` 的提交分支只有三条路：控制 ACK（`0x2001`/`0x200f`）、manager setup（`0x900e`），其余一律 `unsupported++` 然后静默丢弃。

新增第四条：**任意命令都给回包**，回包 opcode = `原 opcode | 0xb000`。

这个关系不是我编的 —— 是固件自己在 setup 路径上用的：

```
固件发 0x900e  →  模型回 0xb00e   （两值都在实测 trace 里出现过）
```

参数**原样回显**，不写任何 guest 内存，不声称任何检测结果。所以它买到的是**协议完整性**，不是"假装有检测结果"。

### 端到端验证（`cvp_compute_test.py`）

固件在任何可达路径上都不发 CVP 命令，所以测试从外部驱动：用 gdbstub 往 TX 窗口写 3 个 word，再写 submit=2。

```
command [00001234 00010000 00000007]
  compute-requests    = 1
  compute-completions = 1
  compute-dropped     = 0
  unsupported-commands= 0
  log: z62.cvp compute completion [0000b234 00010000 00000007] n=1
结果: PASS
```

`0x1234 | 0xb000 = 0xb234`，符合预期。

### 一个意外收获：固件的 CVP 中断是活的

测试里 RX 始终读不到包 —— 因为**固件的 IRQ101 ISR 抢先读走了三个字并清空了 FIFO**（这也解释了为什么 IRQ status 读回 0）。

也就是说：`0x1024295d` 那个 handler 是完整可用的，只是从来没人给它发命令。这比"固件不支持 CVP"的旧判断更准确。

### 诚实的边界

- **没有 payload**：不回传检测框、不写图像、不写结果块
- **正确性无参照**：即使将来固件发命令，回的也只是回显，不是真实结果
- 仍属 `model_pass`，`hardware_acceptance` = **U**

### 开关与观测

| 项 | 值 |
|---|---|
| `nikon-z62-cvp-control.laboratory-compute` | 默认 **off**（保持旧行为） |
| `nikon-z62-cvp-control.compute-delay-ns` | 默认 2 ms（实验室值） |
| `compute-requests` / `compute-completions` / `compute-dropped` | QOM 计数器，测试用 |
| `Z62_CVP_TRACE=1` | 记录每次读写（上限 120 条） |

---

## 二、时序：从"只能看"变成"能标定"，并因此修掉两个真 bug

### 机制

| 文件 | 作用 |
|---|---|
| `body_timing.json` | 真机测量表：每条含 `body_value` / `unit` / `model_event` / `qemu_property` / `status` |
| `timing_probe.py --emit` | 把有真机值且有旋钮的条目转成 `-global` 片段 |
| `timing_probe.py`（默认） | 报告：模型实际间隔 + 标定表（OK / TODO / N/A） |

原则：**标定是改数据，不是改模型**。`body_value` 为 `null` 就老实写"未测"，不猜。

### 第一次真实标定：`capseq-delay-ns = 6100000`

真机 T1→T2 = 6.1 ms，模型原来用 200 µs（为了赶在 1.34 ms 的请求脉冲内完成）。

按 6.1 ms 跑，**画面全黑、`complete` 事件 0 次**。逐层查下去是两个结构缺陷：

| # | 缺陷 | 位置 | 后果 |
|---|---|---|---|
| 1 | 请求脉冲下降沿直接 `timer_del` 取消已锁存的拍摄 | `capseq_output` | 6.1 ms 曝光永远被丢弃 |
| 2 | `capseq_tick` 要求 `capseq_request` 仍为高才允许完成 | `capseq_tick` | 脉冲落下后 timer 到期也不完成 |

两处都修了（新增 `request-fall-latched` 语义：只有**新请求**能重启，下降沿不取消）。

修完实测：

```
request-rise         933249648
request-fall-latched 935109149   (+1.86 ms，脉冲结束但拍摄保留)
complete             939349648   (+6.100 ms ← 精确等于真机值)
rise->complete = 6.100 ms  (body ref 6.1 ms)
```

**这是本轮最有价值的一条**：标定不只是填数字，它把两个"200 µs 下永远看不到"的缺陷逼出来了。而且这两个缺陷的表现正是**黑屏**——和 V24 那类现象同型。

### 一条被我自己推翻的结论（必须记录）

中途我测出"擦除 15 ms 正常、20 ms 必黑"，并写进了代码注释和测量表，准备当成"固件容忍阈值"。

**这是错的。** 用独立端口复测发现：20 ms 下**只是启动更慢**，70 s 时画面正常 1296。

```
t= 55s  nonblack=0      ← 我在这里截图
t= 70s  nonblack=1296   ← 实际只是还没画到
```

原因是擦除期间控制器轮询烧掉虚拟时间，引擎等它，于是**挂墙时间**被拉长。固定 55 s 截图看起来像"启动坏了"。

已把默认值改回 20 ms，并把这个坑写进了代码注释和 `body_timing.json`：

> 提高擦除时长会让 HUD 在挂墙时间上更晚出现；固定 deadline 的截图不能当作"启动失败"的证据，要用更晚的截图复核。

（同类坑在连续多次跑 QEMU 复用默认端口时也会出现，表现为随机黑屏 —— 复测请用 `--port-base`。）

### 标定表当前状态

| 测量项 | 真机值 | 模型 | 状态 |
|---|---|---|---|
| T1→T2 | 6.1 ms | 6.100 ms | ✅ **已标定并验证** |
| T1 间隔 | 55 / 23 µs | — | N/A（模型无对应事件） |
| peer→握手 | 1.1 ms | — | N/A（同 tick 完成，无间隔可标） |
| CVP 命令完成 | 未测 | 2 ms | TODO |
| flash 扇区擦除 | 未测 | 20 ms | TODO |
| flash 字编程 | 未测 | 100 µs | TODO |
| 快门行程 | 未测 | 4 ms | TODO |
| sensor 初始化 | 未测 | 250 ms | TODO |

"N/A" 不是待办，是结论：模型连能表达那个间隔的事件都没有。

---

## 三、回归

| 路径 | 结果 |
|---|---|
| 实验室参数 | 1366 |
| 真参数 + live view | 1296（70 s 起） |
| combo45（PTP 补丁） | 1366 |
| 200 µs 拍照（旧路径） | 正常 |
| `gate_regression.py` | **PASS**（starve 仍精确停在 `0x10200e60`） |

---

## 四、复现命令

```bash
cd D:/z6ii_fw_lab/work/qemu_build

# CVP 回包单元测试
python cvp_compute_test.py --at 28

# 标定：看一眼表
python timing_probe.py --emit

# 用标定值跑（6.1 ms）
python board_probe.py --main-screen --at 55,65 --menu-start 48 --menu-every 0 \
  --port-base 4700 --out-dir run/screen/cal \
  --aux ".local/parameters_real/group17.bin@0x132c0000,,.local/parameters_real/group18.bin@0x132e0000,,.local/parameters_real/group32.bin@0x13e80000" \
  --extra "nikon-z62-lcd.live-view-fill=on,nikon-z72-boot-config.laboratory-capseq-trace=on,nikon-z72-boot-config.laboratory-capseq-actuator=on,nikon-z72-boot-config.capseq-delay-ns=6100000"
```
