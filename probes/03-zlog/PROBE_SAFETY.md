# 探针安全审计 · ZLOG8 线

日期：2026-10-30（更新）　范围：`work/zlog/`　目的：排查「探针写错扇区导致变砖」风险

## 结论摘要

1. **无扇区写能力**：全目录没有任何 `r+b`/`ab`、`seek()+write`、`dd`、mtools、块设备引用；
   不碰 NOR、不碰卡引导扇区/MBR/DBR/分区表。所有落盘都是宿主普通文件的整文件写。
2. **已封堵的变砖路径**：`--out` / `--profile` / `--card-dir` 原先接受任意路径，若指向
   `\\.\PHYSICALDRIVE*` 或 raw 卡镜像（`*.img/*.bin/*.raw/…`）会以"文件"方式**从偏移 0 覆盖**，
   当场毁掉引导结构。本轮加了 `assert_safe_target` / `assert_card_dir`，4 个负例实测全部拒绝。
3. **容器模型已按真实样本更正**（本轮重大变更）：不再有 `0x043A0080` magic，曲线 **257 点**、
   满量程 **0x7FFF**。此前的"256 点 / 0..65535 / 块尾 2 字节用途未定"三项均为错误记录。
4. **color 线的样本与解析器只读**：使用点全部是 `open(..., "rb")` 与 `import`，
   mtime 保持 10-27，未被本轮任何脚本写入。

---

## 1. 重跑结果（全部 exit=0）

| 命令 | 退出码 | 关键数字 |
| --- | --- | --- |
| `python selfcheck.py` | 0 | **34/34 通过**（原 22 项 + NP3 真实模型 7 项 + 互读 2 项 + 原生布局 4 项 − 旧 NP3 项 1 项） |
| `python run_pipeline.py selftest` | 0 | 单调非降 True；范围 16..235；18% 灰卡 code **127**；平台 66（趾部 23）；解析闭环 max 0.000000%；主段量化闭环 0.789%/median 0.384% |
| `python np3_gen.py --out out/ZLOG8.NP3` | 0 | **834 B（0x342，v1.70 原生紧凑布局）**、23 个 tag、name `ZLOG8`、version `0210`、曲线 **2056..30197**（≤0x7FFF）单调、tag2@`0xF4`、表@`0x13C`；sha256 `ab8e6777cde4…46484688` |
| `python np3_gen.py --inspect out/ZLOG8.NP3` | 0 | 7 项结构检查全 OK，loader 模拟无「拒绝」 |
| `python np3_gen.py --inspect …/PICCON219.NP3` | 0 | 真实样本解析 OK：1234 B / 0210 / `A-X rossandhisjpegs` / 257 点 1285..29298 |
| `python np3_gen.py --calibrate …/PICCON219.NP3 --profile out/np3_profile_from_sample.json` | 0 | curve_points=257、value_domain=0x7FFF、34 个 tag 顺序、64B 块头全部落档 |
| `python np3_emu_probe.py 0xC8` | 0 | 路径实测 `B:\NIKON\CUSTOMPC\PICCON01.NP3` |

NP3 相关的核心实测数字：

- 块长恒等式 **`0x242 = 0x40 + 257×2`** 成立 —— 这解释了旧模型里"块尾余出 2 字节用途未定"，
  那 2 字节其实是**第 257 个点**，不是校验字段。
- 曲线块头 `49 30 00 ff 0a e4 01 00`，锚点/元数据区 `06 00 00 2e 16 62 55 9c a4 e4 ef ff` + 补零。
- **互读**：color 线 `z6ii_np3.py` 解析我的产物得到 version `0210` / name `ZLOG8` / 34 tags /
  曲线 tag 在末 / 257 点**逐点一致**；反向解析真实样本的 version、name 与我的解析器结论相同。

曲线量化：`BE16 = round(norm × 0x7FFF)`，norm = `encode(bt709_eotf(u·255))/255`，节点 `u = i/256`（i=0..256）。
自产物曲线 2056（=16/255 域黑位）…30197（=235/255 域满量程）。

---

## 2. 写目标审计

### 2.1 写点清单

| 文件 | 写点 | 目标 | 防护 |
| --- | --- | --- | --- |
| `pipeline.py:66-74,101` | `open(...,'w')`、`np.savetxt` | `out/*.cube`、`out/zlog8_curve_256.txt` | 宿主文本 |
| `pipeline.py:write_luts` | `open(...,'w')` | `out/zlog8_curve_257.txt` | 宿主文本 |
| `np3_gen.py --out` | `open(...,'wb')` | `out/ZLOG8.NP3` | **`assert_safe_target`** |
| `np3_gen.py --card-dir` | `os.makedirs` + `open(...,'wb')` | `<挂载点>/NIKON/CUSTOMPC/PICCONnn.NP3` | **`assert_card_dir` + `assert_safe_target(require_parent=False)`** |
| `np3_gen.py --calibrate` | `json.dump(open(...,'w'))` | `out/np3_profile_from_sample.json` | **`assert_safe_target`** |
| `run_pipeline.py:42,46,49` | `np.save` / `Image.save` | `.npy`/`.png`/`.tif` 帧输出 | 宿主文件 |
| `selfcheck.py:233` | `open(...,'w')` | `VERIFY.md` | 宿主文件 |
| `plot.py:6,101` | `os.makedirs` / `open(...,'w')` | 会话 visualization 目录 | 宿主文件 |
| `np3_emu_probe.py` 全部 `mem_write` | — | **unicorn 模拟内存**（0x10000000 代码页、0xC188xxxx RAM 页） | 不落盘，进程退出即消失 |

### 2.2 只读输入（必须证明没被写）

| 输入 | 用途 | 使用方式 | sha256 | mtime |
| --- | --- | --- | --- | --- |
| `all_modules/Z6_2_0170/eg1850_mas_01700` | 反汇编/模拟执行 | `open(...,'rb')` 唯一 | —（固件，不入 git） | 未变 |
| `work/color/tmpout/card/PICCON219.NP3` | 结构模板/定标 | `open(...,'rb')`（2 处，均只读） | `f15e534eada2…c939a742` | 10-27 11:04 |
| `work/color/z6ii_np3.py` | 互读校验 | `import`（sys.path 注入，只读） | `3b7920e56a20…36214485` | 10-27 11:03 |
| `probes/`（含 `probes/03-zlog/`） | 主线汇总区 | **不访问**，且已列入写入黑名单 | — | 未变 |

代码证据（`grep -nE "DEFAULT_SAMPLE\|from_sample\|PICCON219\|z6ii_np3" *.py`）：
所有命中要么是文档注释，要么是 `open(…, "rb")` / `os.path.exists` / `import`，**无一以这些路径为写目标**。
两者 mtime 均停留在 10-27，早于本轮全部作业，实证未被改动。

### 2.3 扇区写特征专项

```
grep -nE "r+b|ab'|seek\(|dd |mtools|mcopy|PHYSICALDRIVE|/dev/|0x[0-9a-f]+.*write" *.py
```
结果：`seek` 仅出现在 `np.frombuffer(..., offset=…)` 的**读**上下文；无 `r+b`、无 append 模式、
无 `dd`/mtools/mcopy、无设备路径写、无 `subprocess` 落设备。**不存在任何"算扇区号 + 偏移写"的代码路径。**

---

## 3. 卡写入边界

`--card-dir` **必须是已存在的目录挂载点**，不是镜像文件。

| 传入 | 行为（实测） |
| --- | --- |
| 已存在目录（QEMU `vvfat dir=`、或挂载好的卡） | 在其下建 `NIKON/CUSTOMPC/` 并写 `PICCONnn.NP3` ✅ |
| 不存在的目录 | 拒绝，提示"先挂载"（避免半截路径） |
| 文件（`SPEC.md`） | 拒绝：不是目录 |
| `out/`（合法目录） | 成功建 `out/NIKON/CUSTOMPC/PICCON01.NP3`，与 `--out` 产物逐字节一致 ✅ |
| `--out *.img/*.bin/*.raw/*.iso/…` | **拒绝**（偏移 0 覆盖会毁 MBR/DBR） |
| `--out \\.\PHYSICALDRIVE*`、`/dev/*` | **拒绝**（设备命名空间） |
| `--profile out/bad.img` | **拒绝**（同样校验） |
| `all_modules/`、`fw_*`、`ref_fw/`、`probes/` | **拒绝**（只读参考区黑名单） |

负例实测退出码（本轮复测）：`out/card.img`=1、`out/disk.bin`=1、`\\.\PHYSICALDRIVE1`=1、
`--profile out/bad.img`=1。合法路径 `--out out/ZLOG8.NP3`=0。

> 镜像必须走文件系统层：Windows 用 OsFMount/Dokan，Linux 用 `mount -o loop`，
> **QEMU 直接把目录交给 `vvfat`**（本项目推荐路径，全程不涉及裸镜像写）。

---

## 4. 本轮变更

**① 容器模型按真实样本重写（重大更正）**

| 项 | 旧（错） | 新（实测） |
| --- | --- | --- |
| 头部 | `u32 LE 0x043A0080` magic + `"NCP\0"` | 偏移 0 即 `"NCP\0"`，**无 magic** |
| 曲线点数 | 256 | **257** |
| 曲线满量程 | 65535 | **0x7FFF** |
| 版本字段 | u16 BE | ASCII 4 字节 `"0210"`（版本门前缀须 01/02） |
| 块尾 2 字节 | 用途未定（疑校验） | 第 257 个点 |
| tag 顺序 | 未知 | `0x0100,0x0200,0x0300..0x1e00,0x1f00,0x2000,0x10100,0x0002`，尾部 BE32=0 |
| 产物大小 | 660 B | 1234 B（扩展）→ **834 B（v1.70 原生，产物采用）** |

`0x043A0080` 确实存在于固件字面量池并被显式比较（`0x103ba728`、`0x103bb924`），但那是
`0x103bb6da` 某条路径的入口判据，**读的不是 NP3**。把它当 NP3 魔数是此前的误判。

附带修正一个字节序 bug：`curve_block` 里 `astype(">u2").astype(np.uint16)` 会把大端转回原生小端，
导致写出的表是 LE（表现为自读值域溢出到 0xFF10）。已去掉多余 astype。

**② 版权合规**：模板样本 `0x10100` 内是第三方版权说明文本，生成器**默认替换**为 ZLOG8 自己的说明
（`--no-desc-replace` 可关闭，不建议）。selfcheck 有专项检查，确保产物不携带他人版权声明。

**③ 写目标防护**：`assert_safe_target` / `assert_card_dir`（见 §3），接入 `--out`/`--profile`/`--card-dir`。

**④ 产物唯一写源**：`zlog.py` 的 `__main__` 原先会写出与 pipeline 的 3D cube **同名但内容是 1D** 的文件，
静默覆盖 LUT 并破坏校验。已删除其落盘，产物只由 `pipeline.write_luts()` 生成。

**⑤ 可移植性**：`np3_emu_probe.py` 与 `np3_gen.py` 的固件/样本路径改为自解析仓库根（可用
`Z6II_MAS` 覆盖），换 CWD 不再失败。

---

## 5. 边界结论

1. **QEMU 验不了取景**：仿真机传感器画面全黑，曲线装载成功也看不到色调/动态范围变化。
2. **Manage Picture Control 打不开、列表无自定义项 —— 根因在固件侧**：拼 `PICCONnn.NP3` 路径读卡的
   `0x103bb7b4`，经六种引用方式穷尽搜索全部 0 命中（BL/BLX、绝对 dword × MAS+SLA+ex+vr+tpj、
   ADR/ADD PC、`MOVW#0xb7b4`+`MOVT#0x103b`、BL 目标区间、TBB/TBL 跳转表）。UI 簇 `0x102d6xxx` 的
   13 个下游不含它；列表枚举 `0x103bb64e` 读的 RAM 标志表（`0xc29925e8`，stride 8，99 项）只能由扫描填充。
3. **不能据此断言"只能真机导入"**：官方规格称 Z6 II 支持载入自定义 Picture Control，功能存在而静态
   找不到入口，说明仍有盲区（地址运行期计算，或导入在另一未定位簇）。
4. **5 分钟定论实验（纯只读探针）**：QEMU 里对 `0x103DB7B4` 与 `0x103DB64E`（**运行期 VMA**，
   对应文件偏移 `0x3BB7B4` / `0x3BB64E`；mas 运行期基址 `0x10020000`，本文正文用显示口径
   `+0x10000000`，下断点须再 `+0x20000`）下断点后进入该对话框；并观察 `0xc188b298+0x14` 是否由 0 变 1。
5. **现在的剩余低置信项**（已从"值域/点数"转移到结构细节）：
   - 曲线块 `+0x08..0x3F` 的**锚点/元数据区编码**未完全逆出 → 当前照抄样本字节；若相机校验锚点与表一致，装载可能被拒。
   - 块头 `0ae4` / `0100` 两字段含义未确认。
   - 标量 tag 区（0x0300..0x1600）直接继承样本值，即"沿用该 PC 的锐度/清晰度等设置"；若要中性 PC 需另行取值。
   - 该曲线是否作用于 H.264 录制链，仍需真机灰卡实拍（对回 code 127±2）。

---

### ⑥ 地址口径校正（2026-10-30 下午，本轮）

mas 的**运行期基址是 `0x10020000`**；本目录文档里的 `0x103Bxxxx` 全部是显示口径（文件偏移 + `0x10000000`）。
换算关系 `运行期 = 文件偏移 + 0x10020000 = 显示口径 + 0x20000`。
**此前我给出的 QEMU 断点地址 `0x103bb7b4` / `0x103bb64e` 是显示口径，按运行期应为
`0x103DB7B4` / `0x103DB64E`，否则断点打不中。** 已在 SPEC 开头补口径表。

另回报一处主线换算差异：打开返回码 helper 的文件偏移 `0x4B6710` 对应运行期 **`0x104D6710`**，
不是 `0x104F6710`（后者等于把 `+0x20000` 加了两次）。实证方式：反汇编文件 `0x4B6710` 可见
`ldr r5,[pc,#0x264]` 取到 `0x043A0089`、随后 `subs r5,#9`，与描述吻合；文件 `0x4D6710` 处是
`ldrd/strd` 拷贝代码且不含该常量。`np3_emu_probe.py` 用 `BASE=0x10000000` 建映射属显示口径，
其模拟的是模块内部自洽的 PC 相对取指，**结论与运行期基址无关**，文件名取证结果不受影响。

## 6. 约束遵守

- **未刷机**；未生成待刷固件；未触碰 `Z6_2_0170.bin`。
- 未写 NOR、未写卡引导扇区/MBR/DBR/分区表、无任何扇区偏移写。
- 未修改 `probes/`（含 `probes/03-zlog/`）、`dist/`、`.local/`、`work/color/`（样本与解析器只读，mtime 佐证）。
- 密钥未写入本线任何文件；K8 经环境变量 `Z6II_K8` 提供，根 `.gitignore` 已排除固件与 `work/k8/`。
