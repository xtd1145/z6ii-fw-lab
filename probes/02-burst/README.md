# 02 · 连拍规格取证探针（会话 a09d95d5）

## 用途

回答 Z6 II 连拍（DX45 JPEG C4 / DX30 RAW 12-bit）的运行期窗口、帧周期、压缩模式与缓冲帧数。
探针分 P0–P5，**全部只读或监视，不写相机**。

## 文件

- `BURST_SPEC.md` — 最终版规格（可行档位、4K60 推算、证据等级）。
- `PROBE_DESIGN.md` — P0–P5 地址/条件/场景/签名/回传格式。
- `P0_P3_REANALYSIS.md` — 真实 dump 的重新解析（基址 0x10020000、任务表、视频表、对象注册表）。
- `REPORT.md` / `workorder.json` — 底层分析与工作单。
- `compute.py` / `budget.json` — DX30/DX45 预算，可复跑（与仓库结果逐字节一致）。
- `dx45*.py` / `*.txt` — 早期模式表/几何探针脚本与输出。

## 探针（P0–P5）

| 探针 | 类型 | 写？ |
|---|---|---|
| P0 dump `0x1244E7A0` / `0x119FF50C` / registry / arena / 视频表 / 尺寸表 | HMP `memsave` 到文件 | 只写宿主文件 |
| P1 8 个 SIF 运行期入口 `0x1032C77E..CC66` | `-d in_asm` + dfilter | 无 |
| P2 写监视 `0x119DF50C..0x11A0100C` | 监视（不写） | 无 |
| P3 全 RAM 搜名字 `CAPTG/CAPSEQ/SHUTTER/STGRAW` | 搜索 | 无 |
| P4 读视频/尺寸表 | 读取 | 无 |
| P5 MMIO 写回退 `0x20000000..0x25000000` | 监视（不写） | 无 |

## 使用（QEMU）

```bash
# 直接复用主线的单机启动与 dump 工具
python work/boot_one.py <package> --tag burst      # 只做冷启动健康检查
# dump 一律用 HMP memsave（QMP pmemsave 在本 machine 上全 0，不可用）
#   memsave 0x1244E7A0 0x690 mas_task_desc.bin
#   memsave 0x1244E4D0 0x400  mas_obj_registry.bin
#   memsave 0xC2A40000 0x10000 heap_arena.bin
# PC trace：-d in_asm -dfilter 0x1032C77E+0x20,...
```

`compute.py`：
```bash
cd work/dx30_raw && python compute.py    # 重算 budget.json（期望 sha 603a856abb1c…）
```

## 写入目标

- 只有宿主文件（dump/报告）；**相机与卡零写入**。
- 真机采集也是只读：探针 hook 只记录 `r0/r1/[r0+0xC]` 与 MMIO 访问，不改寄存器。

**扇区风险：无。** 不写 NOR、不写卡、不刷机。

## 已排除的路径（仿真）

DX 切换、LV（默认开启）、Movie 模式（物理拨杆）、Release mode（c/E+拨盘）、
GPIO pad 128 线全扫、G14/G15——都不触发快门链；**CAPTG 需要实际拍摄序列，转真机**。

## 回滚

删除 dump 文件即可；相机无状态残留。

## 数据地址修正（2026-10-30）

`mode_records.bin` 已按正确运行期地址 **0x119FF50C** 重新从 `ram_10000000.bin` 截取
（6×0x480，地址 = 0x10000000 + 0x19FF50C）；旧的 0x119DF50C 版本作废（少 0x20000）。
复核脚本 `probe_recheck.py` 可只读重跑 P0/P3/P4 与静态 P1，当前 PASS。

## B1 分页导出（真实 NP3 格式，2026-10-30 更新）

- 真实曲线块：tag 0x0002、len 0x242、块头 0x40、数据区 **257×BE16=514 B**、值域 **0..0x7FFF**。
- 页头放块头保留区 `payload[0x18:0x28]`（`Z6PD`，`flags.bit2=1`）；fallback 放数据区则净载 498 B。
- 净载：**raw 514 B/次**；单调 carrier 222 B/次（0..0x7FFF，`v[i]=i*128+d[i-1]`）。
- 保存次数（每场景）：精简包 10(raw)/23(单调)；完整包 21/48；
  一次全量模式记录 6,912 B = 14/32 次。
- 9 场景总计：精简 90/207 次，完整 189/432 次 —— **人工保存量很大**，建议先做 3 个关键场景
  （单张 / 5fps / 14fps 12-bit）+ 精简包，约 30–70 次。
- 限制：MMIO 全量 trace 放弃（改 16 个事件计数器）；SIF 只保留环形样本并记 dropped。
