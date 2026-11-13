# 01 · 预拍摄钩子探针（会话 54ed5f2c）

## 用途

在 Z6II mas 的零引用空洞里放 9 个钩子站点（8 个拍摄链 + 1 个 NP3 导出转换器）+ 蹦床/派发器/
核心状态机，用 K8 重签成与原厂同尺寸的完整容器。探针包用于在真机上回答“8 个 capture 相关
函数是否提供帧/码流指针”；2026-10-30 版起，导出链把状态写进相机自己保存的 Picture Control
文件（B1 通道，无调试器取数）。

## 文件

- `hookgen.py` — 钩子层 + 核心生成器（布局、THUNK/FALLBACK/DISPATCH、写目标静态检查）。
- `test_core.py` / `stubemu.py` / `test_hooks.py` / `test_rearm.py` — Unicorn A/B 双执行验证。
- `verify_patch.py` — 容器级验证 + 在容器解出的 mas 上复跑 A/B。
- `build_pkg.py` — 用 `Z6II_K8` 重签完整容器并回读比对。
- `np3_decode.py` — 解码 B1 导出的 `Z6PD` 页（STATUS + RING）。
- `BUILD_B1.md` — B1 导出通道：钩点、页协议、用户步骤、限制。
- `rearm.py` / `holes.py` / `semsite.py` — 空洞定位与被覆盖指令重锚定分析。
- `BUILD.md` — 布局、ABI、钩点表、§13/§15 真机采集清单、§16 仿真结论。
- `imm_scan.py` / `scan_targets.py` / `verify_offsets.py` / `indep_verify.py` — 早期静态取证。

## 使用

```bash
cd work/preshoot_build
python test_core.py          # A/B1/B2/B3/B4/B5，期望全绿
python verify_patch.py       # 容器签名/CRC + 载荷 A/B（需要 Z6II_K8）
python build_pkg.py          # 重新生成签名容器（需要 Z6II_K8）
```

QEMU 冷启动复测用 `probes/05-qemu-lab/boot_one.py`：
```bash
python work/boot_one.py work/preshoot_build/Z6_2_0170_preshoot_probe.bin --tag preshoot
```

## 写入目标

| 写入 | 位置 | 说明 |
|---|---|---|
| 生成/验证的输出 | `work/preshoot_build/*.bin`（新文件名） | **只写副本文件** |
| 运行期状态 | DRAM `0xC5EFF000`（STATUS）、`0xC5EFF100`（RING） | 仿真内存 |
| 代码 | mas 空洞（只读映射，仅执行） | 不写 |

**扇区风险：无。** 不接触 NOR 裸偏移、不写分区表、不刷机。真机的“刷入”只允许走官方完整容器
升级流程，且必须由你明确同意。

## 预期输出

- A：CTRL 清零时逐寄存器等同原厂；
- B1：核心启用时退出地址与 r0–r11/sp/lr/NZCV 等同原厂；
- B5：4 个 chunk 头/长度/偏移/sum/version 全对，合并 864 B == STATUS+RING；
  槽位 01 / 长度 0x342 / 非 NCP 缓冲三种门控负例全部不写；
- 容器 49,002,888 B、K8 重签（摘要 `8d96b792…`）、包 CRC `cf9a`；
- QEMU 冷启动：B1-v2 包 PASS（guest 2.41 s、上传 304、渲染 290、HUD 37、无 Data Abort），
  启动期写卡不命中 96–99，`np3_writes` 保持 0，`0xC5EFF000` magic `0x5350365A`。

## 回滚

删除新生成的 `*.bin` 副本即可；相机/QEMU 未受影响。
