# 探针安全与防变砖规范

## 1. 什么是“写错扇区”

相机固件在 NOR（本机 64 MiB，`.local/nor_candidate/NOR_64MiB.bin`）里有引导、参数组、
签名容器与校验字。**任何对 NOR 裸偏移、分区表、固件槽的直接写/擦**都可能让相机无法启动（变砖）。
安全边界：

| 类别 | 允许？ | 说明 |
|---|---|---|
| 写副本文件（`.bin/.bi` 于 `work/`、`dist/`） | ✅ | 所有探针默认方式 |
| 写卡上普通文件（`NIKON\CUSTOMPC\*.NP3`、`DCIM\...`） | ✅ | 文件级写入，不动 FAT 引导扇区 |
| QEMU 内存写（RAM/DRAM 窗口、状态块） | ✅ | 仅仿真，断电即消失 |
| 写 NOR 裸扇区 / 擦块 / 改分区表 | ❌ | **禁止**，除非有专门工具与明确授权 |
| 通过 USB/PTP 直接写固件区 | ❌ | 禁止；只走官方容器的完整刷写流程 |
| 对卡做 `format` / 改 MBR/BPB | ❌ | 禁止；只允许在已有 FAT 上增删文件 |

## 2. 每个探针必须声明的字段

- **写入目标**：具体路径（或内存地址范围）。
- **写入方式**：文件覆盖 / 卡上新增文件 / 内存写。
- **回滚**：恢复原厂文件、删除新增文件、重启 QEMU。
- **扇区风险**：是否触碰 NOR/分区表/引导扇区（默认“无”）。

## 3. 已汇总探针的写入目标（速查）

| 工作流 | 写入目标 | 扇区风险 |
|---|---|---|
| 01-preshoot | `work/preshoot_build/*.bin`（新文件名，不覆盖原包）；运行期写 DRAM `0xC5EFF000..0xC5EFF400`（STATUS/RING/页暂存）与相机导出暂存区 RAM `0xC29AF768`（仅当 r0 等于该地址，512 B 表项） | 无（不刷机；卡上 NP3 由相机自己的保存动作写出） |
| 02-burst | 只读 dump（`memsave` 到文件）与监视；**无写操作** | 无 |
| 03-zlog | `out/ZLOG8.NP3` 与卡上 `NIKON\CUSTOMPC\PICCONnn.NP3` | 无（文件级） |
| 04-beep | `dist/**` 下的全尺寸签名容器副本 | 无（不刷机） |
| 05-qemu-lab | QEMU 进程 RAM；`/api/variant` 的 selections 记录 | 无 |

## 4. 复核机制

每个工作流的对应代理在交付前必须**重新跑一遍探针**，并产出 `PROBE_SAFETY.md`：

1. 用 `grep -n` 列出所有写调用（`open(...,'wb'/'r+b')`、`write`、`pwrite`、`dd`、`mtools` 等）；
2. 对每条写调用标注目标路径/地址，确认不在 NOR/分区表/引导扇区；
3. 实跑一次探针，记录实际产生的文件与哈希（前后对比）；
4. 若发现潜在扇区写入，修掉脚本并重跑，记录 diff。

主线的复核结论统一记录在各子目录的 `PROBE_SAFETY.md` 与 `probes/SAFETY.md` 的附录。

## 附录 A · 2026-10-30 代理复核结论

| 工作流 | 重跑 | 扇区写风险 | 处置 | 报告 |
|---|---|---|---|---|
| 01-preshoot | hookgen/test_core/verify_patch/build_pkg/qemu_check/boot_one 全 exit=0；QEMU 回归 PASS（guest 2.6/324/308，magic 0x5350365A，0 abort） | **无**：写目标只有 `work/preshoot_build/**` 副本与 `.local/webui/run/**` | 源码修复与扇区无关：硬编码 K8 → `Z6II_K8` 加载器；重出包 sha 不变 | `01-preshoot/PROBE_SAFETY.md` |
| 02-burst | compute.py 重跑（budget.json 逐字节一致）、P0/P3/P4 重解析 | **无**：只写宿主 dump/报告；P2/P5 是写监视不发写 | 未改脚本 | `02-burst/PROBE_SAFETY.md` |
| 03-zlog | selfcheck 22/22、selftest、np3_gen、np3_emu_probe 全 exit=0 | **修复了一处真实误用**：`--out/--card-dir` 原可指向卡镜像/设备而从偏移 0 覆盖；已加 `assert_safe_target` / `assert_card_dir`，负例实测拒绝 `*.img/*.raw/*.bin`、`\.\PHYSICALDRIVE*`、`/dev/*` | `np3_gen.py` 已更新并同步；另修两处真实缺陷：`zlog.py` 的 1D cube 会覆盖 pipeline 的 3D 同名产物、`np3_emu_probe.py` 相对路径换目录即坏；均已同步到本目录 | `03-zlog/PROBE_SAFETY.md` |
| 04-beep | 变体/矩阵重跑 | **无**：NOR 镜像 sha256/mtime 前后一致，0 处脚本修改 | 未改脚本 | `04-beep/PROBE_SAFETY.md` |
| 05-qemu-lab | 主线 grep + 运行验证 | **无**：只写 `.local/**` 与 QEMU 客体内存 | 未改脚本 | `05-qemu-lab/PROBE_SAFETY.md` |

**全局结论**：本次复核发现并修复的唯一真实变砖路径是 ZLOG 的 `--out/--card-dir` 任意路径
（可能被误指到卡镜像/裸设备）；其余工作流只有宿主文件/内存写入。任何探针均不写 NOR 裸扇区、
分区表或卡引导扇区。

## 附录 B · 服务端路径穿越修复（2026-10-30）

04-beep 审计发现：`/api/upload` 的 `slot` 取自查询参数且未消毒，`slot=../../..` 可从
`.local/webui/inputs/` 逃逸，理论上足以覆盖 `.local/nor_candidate/NOR_64MiB.bin`（真 NOR 镜像）。

- **已修复**：`slot` 现在必须匹配 `[A-Za-z0-9_-]{1,16}`，否则直接返回 `bad slot`；
  文件名本就经过 `[^A-Za-z0-9._-] → _` 消毒。
- 生效条件：需要重启 8642 控制台（当前有机体在跑，待空闲后重启）。
- 真 NOR 镜像当前 sha256 前缀 `f5fe8fab…`，与项目基线一致，未被改动。
- 触发面：服务只监听 `127.0.0.1`，且本工作流脚本不调用 `/api/upload`；但属于必须堵的写错路径。
- **已生效并实测**：8642 控制台已重启（build 20261030-0212）；`slot=../../evil` 返回 `{"ok": false, "error": "bad slot"}`，未产生任何逃逸文件。
