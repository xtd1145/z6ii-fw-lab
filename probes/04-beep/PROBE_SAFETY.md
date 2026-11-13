# 04-beep 探针复核：写目标审计与防变砖结论

复核时间：2026-10-30 · 会话 2339332f · 范围：`work/beep_fix/` 全部脚本 + 其触发的 `z62_webui` 服务端路径
基线口径：`probes/README.md` 全局安全规则 1–4、`probes/SAFETY.md` §1 允许/禁止表

## 1. 结论（先说结果）

| 判据 | 结果 |
|---|---|
| 是否存在 NOR 裸扇区/擦块写 | **无** |
| 是否存在分区表 / 引导扇区 / MBR-BPB 写 | **无** |
| 是否存在块设备句柄、`dd`、`mtools`、`win32file`、`pwrite/ioctl` | **无**（含注释行） |
| 真机 NOR 镜像是否被改动 | **未改动**（mtime 与 sha256 前后一致，见 §5） |
| 本工作流全部写调用的落点 | `dist/**` 全尺寸副本 + `.local/webui/**` 仿真状态/临时目录 + 本目录日志 |
| 需要修的扇区写 | **无 → 未修改任何脚本**（0 处 diff） |

一句话：本工作流的探针只做「副本文件写 + QEMU 仿真内存写」，符合 `SAFETY.md` 的允许列；
没有发现需要修掉的扇区级写入，因此本轮**没有对任何脚本做改动**。

## 2. 写目标表（逐条 grep 证据）

`write_bytes` / `write_text` / `open(...,'w')` 的完整枚举。目标列均为绝对可核对的字面路径。

| 脚本 | 行 | 写调用 | 目标 | 判定 |
|---|---|---|---|---|
| `make_variants.py` | 194 | `out.write_bytes(reseal(...))` | `ROOT/dist/<probe名>/EG185070.BIN` | ✅ 副本 |
| `make_variants.py` | 212 | `out.write_bytes(data)` | `ROOT/dist/{noop2,beep2}/EG185070.BIN` | ✅ 副本 |
| `make_beep5.py` | 93 | `OUT.write_bytes(out)` | `ROOT/dist/beep5/EG185070.BIN` | ✅ 副本 |
| `make_beep6.py` | 106 | `out.write_bytes(mv.reseal(...))` | `ROOT/dist/{beep6,noop6}/EG185070.BIN` | ✅ 副本 |
| `make_trace.py` | 110 | `out.write_bytes(data)` | `ROOT/dist/tracequeue/EG185070.BIN` | ✅ 副本 |
| `make_doorbell_probe.py` | 73 | `out.write_bytes(data)` | `ROOT/dist/doorbell/EG185070.BIN` | ✅ 副本 |
| `make_copy_probe.py` | 79 | `out.write_bytes(data)` | `ROOT/dist/copyprobe/EG185070.BIN` | ✅ 副本 |
| `find_callers.py` | — | 无写调用（只 print） | — | ✅ 只读 |
| `patch_matrix.py` | 107 | `open(args.out,"w")` | `.local/webui/patch_matrix.json`（默认值） | ✅ 结果 JSON |
| `patch_matrix.py` | 32/40 | `urllib.urlopen` | `http://127.0.0.1:8642` GET/POST | ✅ 见 §4 |

读取侧（全部只读，无 `r+b`/`ab`/`w`）：`STOCK.read_bytes()` = `fw_verified/eg1850_mas_01700`（38,750,138），
`BROKEN/SHIPPED.read_bytes()` = `dist/**` 自身产物。`reseal()` 只在内存 `bytearray` 上改尾部 CRC。

`ROOT` 统一为 `Path(__file__).resolve().parents[2]` = `D:\z6ii_fw_lab`，因此 `dist/**` 不可能解析到仓库外。

## 3. grep 证据（命令与命中）

```bash
# (a) 通用写原语 + 扇区/设备关键词
grep -nE "write_bytes|write_text|[^a-z]open\(|shutil|os\.remove|os\.rename|mkdir|makedirs|seek\(|pwrite|ioctl|win32file|NOR|0x04000000|sector" work/beep_fix/*.py
# (b) 注释掉的写调用（用户特别要求）
grep -nE "^\s*#.*(open\(|write_bytes|write_text|\.write\(|dd |mcopy|mformat|seek\(|shutil)" work/beep_fix/*.py z62_webui/console.py   → 空
# (c) 裸设备/句柄/写标志
grep -nE "\\\\.\\|PhysicalDrive|Volume|FSCTL|O_WRONLY|os\.O_" work/beep_fix/*.py z62_webui/console.py                                    → 空
# (d) 外部写工具
grep -nE "\bdd |mtools|mcopy|mmount|diskpart|format\(" work/beep_fix/*.py                                                                → 空
# (e) 二进制写模式 open（排除 json/log）
grep -rnE "open\([^)]*(r\+|a\+|wb|'w'|\"w\")" --include=*.py work/beep_fix z62_webui | grep -viE "\.log|json|md|selections|out\b|report"  → 空
```

(b)(c)(d)(e) 四条为空即是核心证据：**没有二进制写模式的 `open`**，所有固件级写都走
`Path.write_bytes()`，其参数在源码里是字面可枚举的 `dist` 子路径。

## 4. 服务端（`z62_webui/console.py`）审计——矩阵经由 HTTP 触发，故必须一并查

| 位置 | 行为 | 判定 |
|---|---|---|
| `extract_groups()` L195/L203 | `nor.open("rb")` + `stream.seek()` **只读** | ✅ NOR 全程只读 |
| L209 | `(dest/"group%d.bin").write_bytes()` | ✅ 写到 `.local/webui/parameters/`，不回写 NOR |
| L115/L130 `remember()` | `SELECTIONS.write_text()` | ✅ 状态 JSON |
| L300 | `CACHE.write_text()` = `variants.json` | ✅ 缓存 JSON |
| L431–440 | `resident/primary/auxiliary.bin` 写 `RUN/<profile>-<ts>/` | ✅ 每次运行新建临时目录，QEMU I2C 初值 |
| L489 | `-drive ... file=<card>,format=raw,snapshot=on` | ✅ **`snapshot=on`**：卡镜像写只进临时 overlay |
| L448/`-kernel master` | 镜像以 `-kernel` 加载 | ✅ 只读 |
| `/api/variant`（矩阵用） | `describe()`/`diff_runs()` 仅读文件算 hash 与差异 | ✅ 无写 |
| `/api/upload` L802–804 | `name` 经 `re.sub` 消毒，但 **`slot` 未消毒**，随后 `dest.write_bytes(body)` | ⚠️ 见下 |

**架构级隔离（本轮补测）**：guest 唯一可见的 flash 后端是
`nikon-z62-dual-lab,...,controller-flash-data=RUN/<profile>-<ts>/resident.bin`（L445 + L431–434，
每次冷启动新建的 0xC000 合成文件，头 4 字节 `MAKE`）。全仓 grep `pflash|m25p|n25q|s25fl|flash.*file=`
只有这一处命中——**真 NOR 镜像从不作为可写后端挂给 QEMU**。因此即使 guest 内部走完整的
「固件更新写 flash」流程，落点也只是那个临时 `resident.bin`，结构上不可能回写
`.local/nor_candidate/NOR_64MiB.bin`。这是比「脚本没写 NOR」更强的一层保证。

**唯一发现的真实隐患（不属于本工作流的触发路径，未修）**：`/api/upload` 的
`dest = INPUTS / ("%s__%s" % (slot, name))` 中 `slot` 取自查询参数且未消毒，
`slot=../../..` 形式可从 `INPUTS` 逃逸——理论上足以覆盖 `.local/nor_candidate/NOR_64MiB.bin`。
约束条件：只监听 `127.0.0.1`，且 `patch_matrix.py` 只调 `/api/variants`(GET) 与 `/api/variant`(POST)，
**不经 `/api/upload`**，因此本轮重跑不触发它。修它要改主线共享服务端并重启实验台（会影响并发在跑的
01/02/03/05 工作流），故只登记不擅改；建议的收口是一行 `slot` 白名单 + `resolve()` 后断言仍在 `INPUTS` 下。

## 5. NOR 镜像未被动过的实证

```
.local/nor_candidate/NOR_64MiB.bin
mtime   2026-10-19 15:27:50 +0800   size 67108864   （复核当日仍为 10-19，未被打开写）
sha256  f5fe8fabfd9d847a42dc924aedb20f026e4e960714a2086a5289220d10cf682f
```
与项目记录（真机 64 MiB NOR，`f5fe8fab…`）一致。

## 6. 包完整性（重跑后）

22→25 个变体全部为完整尺寸；CRC 独立复算（`binascii.crc_hqx`，BE16 尾部）：

```
所有 dist/**/EG185070.BIN len=38750138  ✅
模块 CRC OK：除 dist/from_d2_modcrc/EG185070.BIN（crc BAD，diff_vs_stock=1）
容器 dist/container/DO-NOT-FLASH_Z6_2_0170_shutterbeep.bin（原名 Z6_2_0170_shutterbeep.bin，已改名防误刷） len=49002888 ✅（独立复验：容器内 mas 与
dist/beep/EG185070.BIN 逐字节一致、5 个模块 CRC 全 OK、包 CRC 自洽于 body[-16]、
头部 20 字节 == SHA1(body‖K8)；密文差异 62 == 头 20 + 跳板非零 36 + 锚点 4 + 模块 CRC 2）
```

`from_d2_modcrc` 的 CRC 失败是**刻意的负样本**，不是损坏产物：它与原厂 `STOCK` 只差 1 个字节，
而那 1 个字节正是尾部 CRC 本身（载荷逐字节相同）。它存在的目的正是验证「改坏模块 CRC 会被拒绝」。

## 7. 重跑记录

### 判据（读 `patch_matrix.one()` 定义，避免误读 PASS）

| 字段 | 含义 | 通过条件 |
|---|---|---|
| `ok` | 冷启动活到 `guest≥2.4` 且进程未退出 | True = **仅证明没炸启动** |
| `cpu0` | 启动后采到的 CPU0 PC | `101034fc` = 死循环自分支（§8），非该值才算正常推进 |
| `hud` / `hud_bytes` | HUD 截图 hash / 字节数 | 有出图 |
| `uploads` | GPU successful-uploads | >0 |
| `menu_changed` | 按 menu 键 14 s 后截图是否变化 | **True 才是「菜单可用」= 功能级 PASS** |

因此下表按两级记：`启动` 与 `菜单`。`ok=True` 但 `menu_changed=False` 的行，
在功能上是 FAIL（与 §8 的 beep/noop 死循环一致）。

### `make_variants.py --probes`（本地重跑，输出即证据）

```
wrote dist\probeA\EG185070.BIN (stub 8 bytes)
wrote dist\probeB\EG185070.BIN (stub 16 bytes)
wrote dist\probeC\EG185070.BIN (stub 20 bytes)
wrote dist\probeD\EG185070.BIN (stub 24 bytes)
wrote dist\beep4\EG185070.BIN (stub 26 bytes)
```
`probeA/B/C` 为本轮新建（复核前快照里没有），`probeD/beep4` 被重建；
dist 变体数 22 → 25，全部尺寸 38,750,138、CRC 复算通过（§6）。

### QEMU 矩阵 `patch_matrix.py --runs 2`

（逐包结果表，跑完填入；原始日志 `work/beep_fix/_matrix_run.log`，JSON `.local/webui/patch_matrix.json`）

## 8. 已知失败项

- **`beep`/`noop` 原始版死循环**：注入 stub 里的 `mrs r4,apsr` + `msr apsr_nzcvqg,r4` 在 guest 里
  触发异常，而异常向量落在 `0x101034fc` 的自分支（`b 0x101034fc`）上，无人处理 → 死转。
  `beep5` 起把 APSR/CPSR 存取整段去掉；`beep6/noop6` 再去掉阻塞调用。
  该结论属「二分定位到字节 + 机制为推断」：bisect 证明哪些字节致命，GE 域读是解释而非独立执行过的实验。
- **`BeepPlay`（VA `0x10768b6e`，`bl` 目标 `0x10768b6f`）判定为阻塞，已从补丁中弃用**——
  它经 `0x10524005` 走同步队列，超时 5000 ms，会挂在快门释放上下文里。
- **镜像基址更正**：mas 实际由 QEMU 以 `-device loader,addr=0x10020000` 加载，
  `file offset = VA - 0x10020000`。早期文档里按 `0x10000000` 标的 VA（含 `0x10748b6e`）
  需整体 +`0x20000`；补丁本身按文件偏移书写，不受影响。
- **仿真机无法触发拍摄**：因此快门音在 lab 里只能验证到「冷启动 / HUD / 菜单可用」，
  听不到声音不等于补丁失败。

## 9. 回滚

`rm -rf dist/<本次生成的变体目录>` 即可；`STOCK`（`fw_verified/`）与 NOR 均未被打开写过。
