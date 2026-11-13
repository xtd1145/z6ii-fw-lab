# 预拍摄探针 · 安全复核（PROBE_SAFETY）

复核日期：2026-10-30　工作流：`work/preshoot_build/`（副本源：`probes/01-preshoot/`）
复核范围：探针重跑、写目标审计、QEMU 回归。

**结论：未发现任何 NOR 裸扇区、分区表、引导扇区的写入。未刷机。**
本轮唯一的源码修复与扇区无关（硬编码 K8 → 密钥加载器），另有 2 个新增检查脚本。

---

## 1. 重跑记录

| # | 命令 | 退出码 | 关键断言 |
|---|---|---|---|
| 1 | `python work/preshoot_build/hookgen.py` | **0** | `hole use f000e01c..f000e542 = 1318 B of 2470 B`；运行时写入门禁 `dispatcher 0 / core_slot 0 / core_work 27` 条 store 全部证明在 `0x10020000..0x12520000` 之外；`patched module differs in 899 bytes` |
| 2 | `python work/preshoot_build/test_core.py` | **0** | `A pass / B1 pass / B2 pass`；B3 三种垃圾预填（全 1 / 随机 / 仅 magic）均正确重建；B4 探针 5 用例全 OK |
| 3 | `python work/preshoot_build/verify_patch.py` | **0** | 头摘要 `c5e4e26b…` MATCH；5 模块 CRC + 包 CRC `cf9a`；mas 载荷 byte-exact；A 64/64、B1 64/64 等同原厂；B2 状态机 OK |
| 4 | `python work/preshoot_build/build_pkg.py` | **0** | 49,002,888 B；`cipher bytes 921 differ`；重出后头摘要仍 `c5e4e26b8bc5a3d89525f90581995600605fa75f` |
| 5 | `python work/preshoot_build/build_pkg.py --out …_r2.bin` | **0** | r2 与 r1 `sha256` 相同（`5a292dd9…`），`cmp` 无差异 → 密钥加载器改动不影响产物 |
| 6 | `python work/preshoot_build/qemu_check.py …_probe.bin` | **0** | 见 §2 |
| 7 | `python probes/05-qemu-lab/boot_one.py …_probe.bin` | 0 | 见 §2（对照，使用主线工具） |

---

## 2. QEMU 冷启动回归

**qemu_check.py（本工作流，新增）**

```
probe-v3 PASS guest=2.6 up=324 rnd=308 px=37 cpu0=10107070
STATUS  magic=0x5350365a count=265 armed=1 frozen=0 retained=48 jpeg_hits=0
        jpeg_slot=0xffffffff jpeg_addr=0xffffffff
aborts  0
```

**boot_one.py（主线工具，对照）**

```
probe-v3 PASS guest=2.46 up=304 rnd=290 px=37 cpu0=10107074 wall=102.2s
```

判据全部满足：guest ≥2.4、上传 ≥250、渲染 ≥200、HUD 非黑 >30、cpu0 ≠ `0x101034fc`、
`0xC5EFF000` magic 存在、QEMU 日志中 `Taking exception 4` / `Data Abort` 计数为 0。

---

## 3. 写目标审计

### 3.1 审计范围与方法

```bash
cd work/preshoot_build
grep -nE "open\(|\.write\(|\.writelines|pwrite|seek\(|write_bytes|write_text|\bdd\b|mtools|mcopy|mkfs|fdisk|parted" *.py
grep -nEi "nor|partition|mbr|bpb|boot ?sector|0x04000000|PhysicalDrive|/dev/" *.py
```

### 3.2 逐条写目标

| # | 位置 | 目标 | 方式 | 扇区风险 |
|---|---|---|---|---|
| 1 | `hookgen.py:549`、`build_pkg.py:42` | `work/preshoot_build/patch_spec.txt` | 文本写 | 无 |
| 2 | `hookgen.py:550`、`test_core.py:281`、`test_hooks.py:107` | `work/preshoot_build/mas_patched.bi` | 二进制写副本 | 无 |
| 3 | `build_pkg.py:48` → `repack_firmware.py` | `work/preshoot_build/Z6_2_0170_preshoot_np3.bin`（默认 `--out` 可为 probe/r2），经 `tmp` + `os.replace` 原子替换 | 二进制写副本 | 无 |
| 4 | `qemu_check.py:50` | `work/preshoot_build/<stem>.mas.bi` | 二进制写副本 | 无 |
| 5 | `verify_patch.py`、`holes.py`、`test_rearm.py`、`hookgen.py:417` | 仅 `open(...,'rb')` 读 `br_mas.pkl` / 镜像 | **只读** | 无 |
| 6 | `stubemu.py`、`semsite.py`、`thumb_enc.py`、`holes.py` | 无文件写（纯内存/纯计算） | — | 无 |

工具链侧（非本工作流脚本直接写，但在复跑中发生）：

| # | 来源 | 目标 | 说明 |
|---|---|---|---|
| 7 | `probes/05-qemu-lab/boot_one.py` | `.local/webui/extracted/<stem>.bi`（38,750,138 B） | mas 副本缓存；主线工具的既有行为 |
| 8 | `z62_webui.console.Machine.start` | `.local/webui/run/z62-dual-<ts>/`（`primary.bin` 256 B、`auxiliary.bin` 256 B、`resident.bin` 48 KB、`qemu.log`） | QEMU 的设备初值/日志，非固件 |
| 9 | QEMU 卡设备 | `.local/sdcard.raw` | 以 `-drive …,snapshot=on` 挂载（`console.py:489`），**客户机写入不落盘** |

### 3.3 grep 原始证据

```
build_pkg.py:42:    spec.write_text('\n'.join(H.specs(writes)) + '\n', encoding='utf-8')
hookgen.py:549:    (HERE / 'patch_spec.txt').write_text('\n'.join(specs(writes)) + '\n', encoding='utf-8')
hookgen.py:550:    (HERE / 'mas_patched.bi').write_bytes(patched)
test_core.py:281:        Path(HERE / 'mas_patched.bi').write_bytes(patched)
test_hooks.py:107:        Path(HERE / 'mas_patched.bi').write_bytes(patched)
qemu_check.py:50:    dest.write_bytes(bytes(body[off:off + length]))
```

NOR / 分区表 / 块设备检索结果：**全部命中均为字符串误报**（`ignore`/`rpartition`/`neither` 里的 “nor”）。无 `dd`、`mtools`、`mkfs`、`fdisk`、`PhysicalDrive`、`/dev/sd*`、`0x04000000`。

### 3.4 关键保护

- `build_pkg.py` 拒绝 `--out` 等于原厂镜像路径；
- `repack_firmware.py` 自带 `refuse_clobber`（需 `--force`）与相同的 realpath 保护；
- 全部镜像写入都在 `work/preshoot_build/` 下的**副本**，原厂 `Z6_2_0170.bin` 只以 `rb` 打开；
- 运行期 DRAM 写入是 `0xC5EFF000` / `0xC5EFF100`（状态块与环形），由构建期静态门禁逐条证明不在镜像映射内；
- B1-v2 版新增的写入只有相机自己交给写卡 API 的 248 B 文件缓冲（descriptor+8 指向、容量 ≥0xF8），
  门禁是「写入长度 == 0xF8 + 路径以 PICCON96..99.NP3 结尾 + 首字 NCP\0」；B5 负例覆盖槽位 01、
  长度 0x342、非 NCP 缓冲三种情况，全部原样放行；
- 相机随后把该缓冲写到卡上 `NIKON\CUSTOMPC\PICCON96..99.NP3`（相机原有的保存动作），不碰 NOR/分区表。

---

## 4. 修复 diff

### 4.1 K8 明文清理（安全，与扇区无关）

`build_pkg.py` 与 `verify_patch.py` 原本硬编码 8 字节签名密钥（`K8 = bytes.fromhex('<K8>')`）。
根 `.gitignore` 靠"排除这两个文件"临时兜底，注释写明"待改为 Z6II_K8 环境变量后解除"。
本轮按要求改完：**只读环境变量，缺失即报错退出，无默认值、无回退文件**。

```diff
--- a/build_pkg.py / verify_patch.py
+++ b/build_pkg.py / verify_patch.py
 import hashlib
+import os

-K8 = bytes.fromhex('<K8>')                  # 明文，已在本文档中以占位符显示
+K8 = bytes.fromhex(os.environ.get('Z6II_K8', ''))
+if len(K8) != 8:
+    raise SystemExit('set Z6II_K8 to the 8-byte signing key (never commit it)')
```

`verify_patch.py` 改动后与主线维护的 `probes/01-preshoot/verify_patch.py` **逐字节相同**
（`diff` 无输出），行为一致。

**缺密钥时的行为（实测）**

| 命令 | 退出码 | 输出 | 副作用 |
|---|---|---|---|
| `Z6II_K8= python work/preshoot_build/verify_patch.py` | **1** | `set Z6II_K8 to the 8-byte signing key (never commit it)` | 无 |
| `Z6II_K8= python work/preshoot_build/build_pkg.py --out /tmp/x.bin` | **1** | 同上 | **未生成任何文件** |

**用真实 K8 重跑（密钥从 `k8.json` 读入环境变量，命令行不含明文）**

```
$ export Z6II_K8=$(python -c "import json;print(json.load(open('k8.json'))['K8'])")
key loaded from k8.json (len=16)

$ python work/preshoot_build/build_pkg.py --out work/preshoot_build/…_r3.bin
build_pkg exit=0
verified    …_r3.bin: 49002888 B, digest c5e4e26b8bc5a3d89525f90581995600605fa75f,
            mas 38750136 B, package CRC cf9a, payload byte-exact

$ python work/preshoot_build/verify_patch.py
verify_patch exit=0
signature   SHA1(body||K8) c5e4e26b8bc5a3d89525f90581995600605fa75f MATCH
structure   5 modules validated, package CRC cf9a OK
A  no core   64/64 identical to stock
B1 core live 64/64 identical to stock (ABI)
B2 state     magic=0x5350365a armed=1 count=7 -> frozen=1 retained=7 OK
RESULT  all checks pass
```

与改动前对照：头摘要、包 CRC、模块 CRC、A/B 结果**完全一致**；`r3` 与 `r1` 的
`sha256` 相同（`5a292dd9…`），`cmp` 无差异 —— 改密钥来源不影响产物。

**"文件内不再含密钥"的证据**

```
$ grep -rn "<8-byte key literal>" work/preshoot_build/ probes/01-preshoot/
（无输出）

$ grep -rn "fromhex('" work/preshoot_build/*.py
（仅剩 os.environ.get('Z6II_K8', '') 形式，无字面量）
```

本报告也不含明文：所有出现处均以 `<K8>` 占位。

**建议（未执行，属共享配置）**：`.gitignore` 中
`work/preshoot_build/build_pkg.py` 与 `work/preshoot_build/verify_patch.py` 两条排除项
现在可以移除（两文件已不含密钥）；`k8.json` 与 `key.json` 的排除应保留。

### 4.2 新增 `qemu_check.py`

主线 `boot_one.py` 不读 STATUS、不扫 abort 日志，且会把 mas 提取进 `.local/`。
新增的本工作流版本：提取到 `work/preshoot_build/`、经 QMP `human-monitor-command` 读
`0xC5EFF000`、扫描 `qemu.log` 的 `Taking exception 4` / `Data Abort`。
开发中修正三处（均为新脚本自身）：`probes/05-qemu-lab` 的 import 路径、guest 采样阈值
`2.4 → 2.6`（避免边界抖动）、HMP 输出的 ANSI 回显改为走 QMP。

### 4.3 未修改的部分

`probes/` 目录**只读**，未做任何改动（`probes/01-preshoot/` 与工作集的 `hookgen.py`、
`rearm.py`、`stubemu.py`、`test_core.py`、`BUILD.md` 逐字节相同；`verify_patch.py` 仅多一个
`k8.json` 回退分支）。
`dist/`、`.local/` 里的其他会话文件未触碰；未刷机；未执行任何 `dd`/`mtools`/分区操作。

---

## 5. 最终声明

1. **不刷机**：本工作流只生成镜像副本，从不向相机、USB/PTP 或任何块设备写入。
2. **不写 NOR**：没有打开过 `.local/nor_candidate/*`，没有裸扇区偏移常量，没有 `dd`/擦块。
3. **不改分区表/引导扇区**：无 MBR/BPB/FAT 相关操作；卡镜像是 `snapshot=on`，客户机写入不落盘。
4. **不改其他会话**：`probes/`、`dist/`、`.local/` 的既有文件均未修改。
5. **密钥不入库**：两个脚本已移除硬编码 K8，改为 `Z6II_K8` 环境变量或 `k8.json`。
