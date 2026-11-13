# 04 · 快门音补丁探针（会话 2339332f）

## 用途

生成快门音相关的固件变体（beep/noop/探针），在 QEMU 里跑矩阵，判断哪类改动能在冷启动后
保持 HUD 与菜单可用。所有产物都是**完整尺寸的签名容器副本**。

## 文件

- `make_variants.py` — 变体生成器（T4/T5 编解码、`reseal()`、`--probes` 生成探针族）。
- `make_trace.py` / `make_doorbell_probe.py` / `make_copy_probe.py` — 队列/门铃/拷贝探针。
- `patch_matrix.py` — 把 `dist/` 变体逐个冷启动，判定 HUD/GPU/死循环。
- `find_callers.py` — 静态调用者检索。
- `audio_path_report.md` / `beep5_report.md` / `beep6_report.md` — 结论与现状。

## 使用

```bash
cd work/beep_fix
python make_variants.py --probes          # 生成探针变体到 dist/
python patch_matrix.py --runs 2           # QEMU 矩阵，结果 .local/webui/patch_matrix.json
```

单包冷启动也可用 `probes/05-qemu-lab/boot_one.py`。

## 写入目标

| 写入 | 位置 | 说明 |
|---|---|---|
| 变体镜像 | `dist/**`（全尺寸副本） | 只写副本文件 |
| 矩阵结果 | `.local/webui/patch_matrix.json` | 宿主文件 |

**扇区风险：无。** 不接触 NOR、不写卡、不刷机。

## 已知现状

- beep/noop 原始版死在 `0x101034fc`（APSR 存取）；beep5 安全但无声；beep6/noop6 已去掉
  APSR 与阻塞调用。
- 仿真机无法触发拍摄，因此快门音只能验证到“启动/HUD/菜单可用”。

## 回滚

删除 `dist/` 下生成的变体即可。

## 真机可刷包（2026-10-30）

| 包 | 大小 | sha256 前缀 | 说明 |
|---|---|---|---|
| `dist/container/Z6_2_0170_beep5_signed.bin` | 49,002,888 | `c7fd9616…` | 注入 20 B、栈操作最少，**建议优先** |
| `dist/container/Z6_2_0170_beep6_signed.bin` | 49,002,888 | `2872fc9f…` | 多 push/pop，冗余无害 |
| `dist/container/DO-NOT-FLASH_Z6_2_0170_shutterbeep.bin` | 49,002,888 | — | **已知死循环，禁止刷入**（已改名） |

**关键事实：beep5 / beep6 都不会发声**——反汇编核实两者都没有 `BeepPlay`/WAV 调用，
且 beep6 与 noop6 的 mas 字节完全相同。真机能验证的是“相机接受自签容器 + 跳板安全”，
不能验证“静音拍摄出声”。

另：包 CRC 恒为 `cf9a` 不是 bug（每个模块段自带 CRC，段贡献为 0），所以包 CRC 对合法补丁
没有区分力；真正的关口只剩头部 SHA1 与 12 个保留字节。

真机步骤/回滚/风险见 `REAL_CAMERA_BEEP.md`。
