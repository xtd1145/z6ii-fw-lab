# 03 · ZLOG8 / NP3 载体探针（会话 0d060783）

## 地址口径（先读）

mas 的**运行期基址是 `0x10020000`**。本目录文档与代码注释里的 `0x103Bxxxx`/`0x104Bxxxx` 都是
**显示口径 = 文件偏移 + `0x10000000`**（`capd.py` 的 `BASE`）。

```
运行期 VMA = 文件偏移 + 0x10020000 = 显示口径 + 0x00020000
```

**给 QEMU / gdb 下断点必须用运行期地址**：卡载扫描入口 `0x103DB7B4`、槽位枚举 `0x103DB64E`、
rev16 `0x103DC792`、TLV 扫描 `0x103DB6DA`、序列化器 `0x103DB862`、保存入口 `0x103DBDD4`、
打开状态 helper `0x104D6710`（对应文件偏移 `0x4B6710`；主线消息里的 `0x104F6710` 多加了一次
`+0x20000`，已用反汇编确认 `0x4B6710` 处才有 `ldr r5,[pc,#0x264]=0x043A0089` + `subs r5,#9`）。
`np3_emu_probe.py` 的模拟执行只依赖模块内部自洽的 PC 相对取指，**结论与运行期基址无关**。

## 用途

把仿 R3D 的 8-bit log 曲线（ZLOG8）做成机内可导入的 NP3 自定义 Picture Control，并提供
主机侧正/逆向管线与 NP3 解析/定标工具。

## 文件

- `SPEC.md` — 曲线公式、量化预算、NP3 容器（**v1.70 原生 834 B** 与样本 1234 B 扩展布局的区分）、置信度表。
- `VERIFY.md` — **34 项**自检实测数字。
- `PROBE_SAFETY.md` — 写目标审计与扇区风险结论。
- `zlog.py` / `pipeline.py` / `run_pipeline.py` — 曲线定义与主机侧管线（含 4 张 33³ cube）。
- `z6ii_np3_real.py` — **v1.70 原生 834 B 布局**的读写 + rev16 语义模拟 + 审计。
- `np3_gen.py` — NP3 生成（`--layout native|extended`）/ `--inspect` / `--calibrate` / `--card-dir`。
- `np3_emu_probe.py` — Unicorn 模拟执行固件扫描入口（取证文件名/路径/A-B 盘符）。
- `selfcheck.py` — 端到端自检（34 项）。
- `samples/PICCON219.NP3` — 真实扩展布局样本（1234 B），作为标量区与 64 B 块头的模板保留。
- `ZLOG8.NP3` — **原生 834 B 产物**；`zlog8_curve_256.txt` / `zlog8_curve_257.txt` — 曲线表。

## 使用

```bash
cd work/zlog
python -c "import pipeline; pipeline.write_luts('out', 33)"   # 曲线 + 4 张 cube + 257 点表
python run_pipeline.py selftest
python run_pipeline.py decode frames/ --bits 8
python np3_gen.py --out out/ZLOG8.NP3                         # 默认 --layout native -> 834 B
python np3_gen.py --inspect out/ZLOG8.NP3                     # 原生审计（偏移/rev16/约束）
python np3_gen.py --inspect samples/PICCON219.NP3             # 1234 B 走通用 TLV 解析
python np3_gen.py --layout extended --out out/ZLOG8_ext.NP3    # 需要扩展形态时
python np3_gen.py --calibrate samples/PICCON219.NP3 --profile profile.json
python np3_gen.py --card-dir "<卡挂载点>" --slot 0xC8          # -> NIKON/CUSTOMPC/PICCON01.NP3
python selfcheck.py
```

卡上文件名已由固件取证确定：`B:\NIKON\CUSTOMPC\PICCON01.NP3`（slot 0xC8→01，全大写 8.3）。
`'A'/'B'` 是盘符（固件 `0x103bc77e` 把 `B:\` 首字符替换成活动卡），**文件内容里不需要卡标识**。

## 写入目标

| 写入 | 位置 | 说明 |
|---|---|---|
| 曲线/cube/NP3 | `work/zlog/out/**` | 宿主文件 |
| 卡上文件 | `<卡挂载点>/NIKON/CUSTOMPC/PICCONnn.NP3` | 文件级写入；不碰 FAT 引导扇区 |

**扇区风险：无。** 禁止 `format`、禁止写卡引导扇区/分区表；真机用 PC 把 NP3 复制进
`\NIKON\CUSTOMPC\` 即可。

## 预期与边界

- 自检 **34/34**；原生 834 B 布局的 15 项审计全过（`0xF4`/`0xFC`/`0x13C`/`0x33E` 实测吻合）。
- **QEMU 里导入不可达**：Manage Picture Control 打不开，扫描 `0x103bb7b4` 与枚举 `0x103bb64e`
  都没执行（trace 仅 86 B）；Set Picture Control 列表只有内置项。
- **取景效果无法在 QEMU 验证**（传感器画面全黑）。真机：导入 → 选中 → 拍 18% 灰卡 →
  读 luma 应落 code 127±2。

## 回滚

删除 `out/` 下生成物与卡上的 `PICCONnn.NP3` 即可。

## 路径守卫（2026-10-30 加固）

`np3_gen.py` 现在强制：

- `--out` 拒绝 `*.img/*.raw/*.bin/*.iso` 等镜像文件与 `\\.\PHYSICALDRIVE*`、`/dev/*`；
- `--card-dir` 必须是**已存在的目录挂载点**（镜像文件一律拒绝，避免从偏移 0 覆盖引导扇区）；
- 拒绝写入 `all_modules/`、`fw_*/`、`ref_fw/`（`probes/` 由本目录同步流程用文件工具写入，不经该守卫）。

负例实测（全部 exit=1）：`--out out/card.img`、`--out out/Z6_2_0170.bin`、`--out out/wvd.raw`。
详见 `PROBE_SAFETY.md`。

## NP3 容器：两种布局（2026-10-30 下午校正）

### `0x043A0080` 不是魔数

它是**「文件打开成功」的返回码**：`0x104B6710` 处 `ldr r5,[pc,#0x264]` 取到 `0x043A0089`，
`subs r5,#9` 后成功路径返回 `0x043A0080`；`bl 0x104B67A8` 之后 `0x103BA5F4` / `0x103BB70C`
两处 `cmp r0,#0x043A0080` 检查的是打开状态。真实文件头只有 `NCP\0`，样本里搜不到该常量是正常的。

### v1.70 原生紧凑布局 = 834 B（`0x342`）— **ZLOG8 产物采用此布局**

`0x103BBDD4` 是保存到卡的入口（内含 `B:\NIKON\CUSTOMPC\` 与 `%s%s%02d.%s`），调用序列化器
`0x103BB862`（`movw r8,#0x242`）。它只写 tag `0x0100`..`0x1600`，因此：

| 偏移 | 内容 |
|---|---|
| `+0x000` | `NCP\0` |
| `+0x004` | tag `0x0100` len 4，ASCII `"0210"` |
| `+0x010` | tag `0x0200` len 20，名称 |
| `+0x02C` | tag `0x0300`..`0x1600` **共 20 个**，各 len 2 `(value,step)` |
| **`+0xF4`** | tag `0x0002`，len `0x242`（`4+12+28+20×10 = 244`） |
| `+0xFC` | body：`+0x00..0x3F` 块头/元数据 |
| **`+0x13C`** | **257 个 BE16 曲线表** |
| `+0x33E` | 4 字节零尾；总长 `0x342 = 834` |

### PICCON219.NP3 的 1234 B 是扩展布局

多出的 `0x1700..0x1E00` 标量、`0x1F00`、`0x2000`、`0x10100` **不是 v1.70 写的**（可能来自别的机型
或 PC 端工具）。读取器按 TLV 扫描找 tag 2，所以扩展布局也能载入，故样本仍作为**标量区与 64 B
块头的取值模板**保留（`--from-sample`，默认启用）。

### 曲线块与值域

`0x242 = 0x40 + 257×2` 精确成立 —— 早先记录的"块尾余出 2 字节用途未定"其实就是**第 257 个点**。
表是 **257 个 BE16**，满量程 **0x7FFF**（15-bit，不是 65535）。

⚠ **第 257 点的字节序陷阱**：`0x103bc792` 的 rev16 循环是 `movw r4,#0x100` + `cmp r1,r4`，
即**只转前 256 个字**。第 257 个字不转换，在相机内存里仍按文件的大端字节存在（小端 CPU 读出为
交换值，例如文件 `0x75F5` → 内存 `0xF575`）。文件里应写正确的 BE 值（与其余 256 点一致）；
主机侧若按"内存语义"读第 257 点，须自行换回。`z6ii_np3_real.simulate_rev16()` 已复现该行为并被
自检断言。

### 当前产物

`ZLOG8.NP3` 834 B / sha256 `ab8e6777cde4fd460ba825ce05def70b548d6848428fb3bcd801a62d46484688` /
tag2 `@+0xF4` / 表 `@+0x13C` / 257 点 `2056..30197` 单调 / `name=ZLOG8` / `version=0210`。
样本里的第三方版权文本（tag `0x10100`）在扩展布局下会被默认替换；原生布局本就不写该 tag。
