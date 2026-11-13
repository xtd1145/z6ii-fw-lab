# 探针重跑与扇区写安全审计

日期：2026-10-30　对象：`work/dx30_raw/` + `probes/02-burst/`（只读参考）　结论：**无扇区写风险；所有探针只读或只监视，只写宿主 dump/报告文件。**

## 0. 结论

- `compute.py` 重跑后 `budget.json` **逐字节一致**：`sha256 603a856abb1cd99152a88c175084168bd9f325025ff728985e90f3017a6e2bd5`。
- P0/P3/P4 用现有 dump 重新解析，**全部 PASS**；P1 的静态处理表 PASS（live trace 是主线 QEMU artifact，本会话不能重跑）。
- 所有探针代码里没有 `dd`、`mtools`、块设备、NOR 偏移、分区表、刷机/写卡路径；只有两个宿主写文件入口：
  - `compute.py` → `budget.json`
  - `deliverable/verify.py` → `verify.txt` / `verify.json`
- P2/P5 是**写监视**，只记录 guest 的写访问，不发起写；已在文档和工作单中明确标成 `monitor write accesses only; no writes issued`。
- CAPTG 仍写明需要实际拍摄序列；仿真机不可达；真机采集清单全部是只读 dump/trace。

## 1. 重跑命令与输出

### 1.1 预算

```bash
cd D:/z6ii_fw_lab
python work/dx30_raw/compute.py >/dev/null
python -c "import hashlib;print(hashlib.sha256(open(r'work/dx30_raw/budget.json','rb').read()).hexdigest())"
```

输出：

```
603a856abb1cd99152a88c175084168bd9f325025ff728985e90f3017a6e2bd5
```

与 `probes/02-burst/budget.json` 及历史值一致。

### 1.2 P0/P1/P3/P4 只读核对

> 本会话没有 QEMU 运行环境；P0/P3/P4 的“重跑”是**重新读取主线已采集的 dump 并逐项校验**，不是重新启动 QEMU 采集。数据与历史一致；若主线要 live 重采，按 `PROBE_DESIGN.md` 的 `memsave` 地址执行即可。

```bash
python work/dx30_raw/probe_recheck.py
```

结果：`RESULT: PASS`。关键 PASS：

```
[PASS] 60 task names                                  ['PBPSUB', 'MRAP_M', 'AUDIOAPL']
[PASS] CAPSEQ=52 CAPTG=53 SHUTTER=54                  CAPSEQ/CAPTG/SHUTTER
[PASS] fields type/size/0x1000/0                      all 60
[PASS] task dump == RAM @0x1244E7A0
[PASS] 53 entries
[PASS] first id 0x530, last id 0x53a                  first=530 last=53a
[PASS] no CAPTG at boot
[PASS] registry dump == RAM @0x1242E4D0
[PASS] nonzero counts 6/3/5/3/6/3                     [6, 3, 5, 3, 6, 3]
[PASS] no width/height/period fields                  []
[PASS] CaptureSensorService=0x02020000
[PASS] CaptureSequenceService=0x04210000
[PASS] name CAPTG              @1244ed6c
[PASS] name CAPSEQ             @1244ed50
[PASS] name SHUTTER            @1244ed88
[PASS] name STGRAW             @1244e9ec
[PASS] name SIF_0              @1078b196
[PASS] name CaptureSensorSer   @1244c254
[PASS] 18 rows
[PASS] 4K30 id=66                                     (66, 0, 30, 3840, 2160)
[PASS] 4K25 id=67                                     (67, 0, 25, 3840, 2160)
[PASS] 4K24 id=68                                     (68, 0, 24, 3840, 2160)
[PASS] no 4K60 in mas table
[PASS] 4K60 row0 3840x2160/60000                      (3840, 2160, 60000)
[PASS] 4K50 row2 3840x2160/50000                      (3840, 2160, 50000)
[PASS] 4K30 row4 3840x2160/30000                      (3840, 2160, 30000)
[PASS] DX L 3936x2624                                 (3936, 2624, 9)
[PASS] DX M 2944x1968                                 (2944, 1968, 10)
[PASS] DX S 1968x1312                                 (1968, 1312, 11)
[PASS] 4K 3840x2160                                   (3840, 2160, 12)
[PASS] 1080 1920x1080                                 (1920, 1080, 13)
[PASS] 31 entries
[PASS] entries 12-19 are SIF
[PASS] 0x1032C9C8 is SIF_5 (index 17)
[PASS] registry_base == registry_dx                   0 dwords
[PASS] arena diff 4 B
[PASS] 0xC2A4A000 page all zero                       0 nonzero
```

### 1.3 发现的唯一数据问题（非安全问题）

`.local/webui/probe/mode_records.bin` 和 `probes/02-burst/mode_records.bin` 都是**旧地址 `0x119DF50C`** 的 dump，不是修正后的 `0x119FF50C`。
正确数据在 `ram_10000000.bin` 的 `0x119FF50C`（6 条稀疏记录，非零 dword 数为 6/3/5/3/6/3）。
这是**读错地址**，不是写；不会写相机/卡/NOR。`probes/02-burst` 是只读参考，未修改；如需独立文件，请按 `PROBE_DESIGN.md` P0 重 dump `0x119FF50C 0x1B00`。

### 1.4 P1 的边界

`0x1032C9C8`（SIF_5）在启动 trace 中命中 1 次，其余 7 个入口 0 命中。
live trace 文件不在 probe 目录，本会话没有 QEMU 运行环境，**不能重跑 trace**；本次重跑的是静态处理表（31 项、SIF 12–19、SIF_5 地址），全部 PASS。

## 2. 扇区写目标审计

### 2.1 目标表

| 探针/脚本 | 操作 | 目标 | 写相机/卡/NOR？ | 证据 |
|---|---|---|---|---|
| P0 `memsave` | 读 guest RAM/表到宿主文件 | 宿主 dump 文件 | **否** | `memsave` 是读；`ram_10000000.bin`/`mas_*.bin` |
| P1 `-d in_asm` + dfilter | 读执行 trace | 宿主 trace/stdout | **否** | trace 只读 |
| P2 写监视 | 监视 guest 写访问 | 宿主 CSV/stdout | **否（只监视，不发起写）** | `PROBE_DESIGN` P2、workorder `action` |
| P3 名字搜索 | 读 RAM 字符串 | 宿主报告 | **否** | `ram.find`；无写 |
| P4 表读取 | 读视频/尺寸表 | 宿主报告 | **否** | `probe_recheck.py` 只读 |
| P5 MMIO 写回退 | 监视 guest 写访问 | 宿主 CSV/stdout | **否（只监视，不发起写）** | `PROBE_DESIGN` P5、workorder `monitor` |
| `compute.py` | 写预算 | 宿主 `budget.json` | **否** | `json.dump(open(...,"w"))` |
| `deliverable/verify.py` | 写验证结果 | 宿主 `verify.txt/json` | **否** | `Path.write_text` |
| `deliverable/*.bin` | 之前会话的打包验证副本 | 宿主文件 | **否** | 未刷机；README 明示 |

### 2.2 grep 证据

```bash
# A. 宿主写入口
grep -rnE "open\([^)]*[\"'](w|wb|a|ab|r\+)[\"']|write_text|json\.dump" probes/02-burst work/dx30_raw --include="*.py"

# B. 块设备/dd/mtools/NOR/分区表
grep -rniE "\bdd\b|mtools|mcopy|/dev/sd|/dev/mmcblk|PhysicalDrive|mkfs|fdisk|sgdisk|sfdisk|parted|flashrom|burn|NOR|partition" probes/02-burst work/dx30_raw --include="*.py" --include="*.md" --include="*.json"

# C. 只读工具证据
grep -rnE "memsave|\bxp\b" probes/02-burst work/dx30_raw --include="*.md" --include="*.json"
```

结果：

- **A** 只命中 4 处宿主写：`probes/02-burst/compute.py`、`work/dx30_raw/compute.py`（各写 `budget.json`），`work/dx30_raw/deliverable/verify.py`（写 `verify.txt/json`）。没有 `wb` 打开块设备。
- `probes/02-burst` 的旧脚本（`adversarial_check.py`、`dump_mode_table.py`、`fps_*.py`、`geom_anchor.py`、`sim_dx45.py`、`verify_mode_table.py`、`xref_*.py`、`fw.py`）只有 `open(..., \"rb\")` 读取 mas/Z8；grep A 未命中任何写入口。
- **B** 没有代码路径命中；命中的只是文档里的“不写 NOR/不写卡/不刷机”政策文字，以及 `JPEG normal` 里的 “norm” 被大小写不敏感搜索误匹配。
- **C** 只有 `memsave`/`xp` 的只读说明。

补充 grep：

```bash
grep -rnE "\.write\(|subprocess|os\.system|popen|shutil\.(copy|move)|os\.remove|unlink|truncate|seek\(" probes/02-burst work/dx30_raw --include="*.py"
grep -rnE "open\(" probes/02-burst work/dx30_raw --include="*.py"
```

- 危险调用只命中 `sim_dx45.py:139` 的 **print 文本**“truncated”，不是实际调用；没有 `.write(`、`subprocess`、`os.system`、`popen`、`shutil`、`unlink`、`truncate`、`seek`。
- 所有 `open()` 里，除 `compute.py` 的 `budget.json` 用 `"w"` 外，其余全是 `"rb"`；`verify.py` 用 `Path.write_text` 写 `verify.txt/json`。

### 2.3 含糊点已明确

- `workorder.json` 的 P5 原来写 `"writes_only": true`，容易被误读为“发起写”。已改为：
  - `"monitor": "write accesses only; no writes issued"`
- P2 增加：
  - `"action": "monitor write accesses only; no writes issued"`
- QEMU 的 breakpoint/watchpoint 走调试寄存器，只停止/记录执行或访问，不写 guest 内存。
- `PROBE_DESIGN.md` P2/P5 均加粗“**只监视 guest 写，不发起任何写**”。
- `BURST_SPEC.md`/`PROBE_DESIGN.md` 顶部加安全边界：只读、只监视、不写 NOR/分区表/卡/相机。

## 3. CAPTG / 真机边界

- `BURST_SPEC.md` §0.1 与 §4 写明：**CAPTG 需要实际拍摄序列；仿真机无法创建**。
- 仿真机已排除：LV 是默认 APPLV（registry 59 行无 CAPTG）、Movie 模式不可切、Release mode 无输入、外接快门 G14/G15 不触发、DX 切换 0 差异。
- 真机采集清单（BURST_SPEC §4）全部是只读：
  - P0/P3/P4：`memsave`/读表；
  - P1：hook 只记录 `r0/r1/[r0+0xC]`；
  - P2/P5：只监视写，不发起写。
- `workorder.json` 的 `status` 已写明 “CAPTG requires an actual shooting sequence; ... Real-camera only.”
- 真机数据导出走 **B1 NP3 分页**：真实曲线块是 tag `0x0002`、len `0x242`、0x40 块头 + 514 B 数据区（257×BE16，0..0x7FFF）；raw 514 B/次，单调 222 B/次。唯一落盘是相机正常的 Picture Control 保存（FAT 上的 NP3 文件），不是裸扇区写；不写 NOR/分区表/引导扇区/固件。方案见 [B1_DUMP_PLAN.md](B1_DUMP_PLAN.md)。

## 4. 本轮修改

- 新增 `work/dx30_raw/probe_recheck.py`（只读重解析，不写文件）。
- 新增 `work/dx30_raw/PROBE_SAFETY.md`（本文件）。
- `workorder.json`：增加 `safety`；P2/P5 明确 monitor-only。
- `PROBE_DESIGN.md`/`BURST_SPEC.md`：安全边界、P2/P5 只监视、mode_records 旧地址提醒。
- **未修改** `probes/`（只读参考）、`dist/`、`.local/`、其他会话目录、原始固件、`key.json`；未刷机。
- `probes/02-burst/` 里的 BURST_SPEC/PROBE_DESIGN/workorder 是本轮审计**之前的快照**；最新安全边界与 P2/P5 措辞在 `work/dx30_raw/`。按目录约定没有改 `probes/`；如需同步请主线复制。

## 5. 最终结论

- **探针写目标全部是宿主 dump/报告文件；相机、SD 卡、NOR、分区表、引导扇区零写入。**
- P2/P5 只监视 guest 写访问，不会把任何写发到相机/卡/NOR。
- 唯一发现是 `probes/02-burst/mode_records.bin` 用了旧地址 `0x119DF50C`，属于**数据正确性**问题，不是写安全问题；正确数据在 `ram_10000000.bin` 的 `0x119FF50C`。
- CAPTG 仍标真机；仿真机路线已停止。
