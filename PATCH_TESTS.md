# 全部补丁包启动测试（2026-10-29）

对 `dist/` 下所有 master 变体与更新容器，加上 `work/preshoot_build/Z6_2_0170_hooks.bin`，
逐包冷启动并判定（客体 ≥ 2.4 s；GPU 上传 ≥ 250、渲染 ≥ 200；HUD 非黑采样 > 30；
cpu0 不在空转地址 `0x101034fc`）。相同字节的包只跑一次。

**结果：18/29 通过。** 原始逐包数据见 `results/variants_try.json`。

## 两个重点会话的包

| 会话 | 产物 | 结果 |
|---|---|---|
| 破解固件添加预拍摄 `54ed5f2c` | `work/preshoot_build/Z6_2_0170_hooks.bin` | **PASS**：guest 2.67 s，上传 334，渲染 317，HUD 正常。`BUILD.md` 明确功能核心尚未实现，启动行为等同原厂（钩子走回退桩）。 |
| 固件多进程抓流 `a09d95d5` | 无可启动固件包，产物是 DX30 RAW / DX45 JPEG 预算与分析（`work/dx30_raw/REPORT.md`、`workorder.json`、`compute.py`） | 复跑 `compute.py`，`budget.json` 与原文件**逐字节一致**（sha256 `603a856abb1c…`）。 |

## 全部结果

### 容器（解出 mas 后启动）

| 包 | 结果 | 说明 |
|---|---|---|
| `Z6_2_0170_shutterbeep.bin` | FAIL | QEMU 在客体 2.4 s 前退出（beep 补丁已知死循环） |
| `decoy/Z6_2_0171_d2_modcrc.bin` | PASS | 上传 312 / HUD 正常 |
| `decoy/Z6_2_0171_d3_pkgcrc.bin` | PASS | 上传 314 / HUD 正常 |
| `decoy/Z6_2_0171_k8zero.bin` | FAIL | QEMU 提前退出（K8 清零包） |
| `EG185070_tv16000.BIN` | PASS | 上传 304 |
| `poc/Z6_2_0170_poc_compressed.bin` | FAIL | QEMU 提前退出 |
| `poc/Z6_2_0171_noop.bin` | FAIL | 上传仅 12，无 HUD（noop 补丁已知问题） |
| `signed/Z6_2_0171_signed.bin` | PASS | 上传 314 |
| `work/preshoot_build/Z6_2_0170_hooks.bin` | PASS | 上传 334 |

### dist/ master 变体

| 包 | 结果 | 包 | 结果 |
|---|---|---|---|
| beep | FAIL（上传 12） | beep4 | FAIL（上传 12） |
| beep5 | PASS（314） | beep6 | PASS（304） |
| bulkprobe | PASS | copyprobe | FAIL（上传 236，无 HUD） |
| doorbell | PASS | noop | FAIL（上传 12） |
| from_d2_modcrc | PASS | from_poc_compressed | PASS |
| from_signed | PASS | from_tv16000 | PASS |
| installprobe | PASS | installprobe2 | FAIL（上传 0） |
| porttest | PASS | probe | PASS |
| probeD | FAIL（上传 12） | srcprobe | PASS |
| statuswriter | FAIL | tracequeue | PASS |

`statuswriter` 复测时 200 s 内未到客体 2.4 s（慢/挂起），未出 HUD。

失败项都是实验性探针/已知坏包（beep 原始版、noop、probeD、installprobe2、
copyprobe、statuswriter 与对应容器），与早前的补丁矩阵结论一致；未见新增回归。

## 2026-10-29 会话修复后复测

### a09d95d5「固件多进程抓流」

- 交付：`work/dx30_raw/deliverable/Z6_2_0170_poc_compressed.bin`（49,002,888 B，sha `afa1527a…`）
  与独立 mas（38,750,138 B，sha `1b42ab7c…`）；补丁点 mas `+0x1091BF8`。
- 冷启动 **PASS**：guest 2.51 s、上传 304、渲染 290、HUD 37；从容器解出的 mas 与独立 mas
  哈希完全一致。
- 端到端 UI：PHOTO SHOOTING MENU → NEF (RAW) recording → NEF (RAW) compression，
  实测显示 `Lossless COMPRESSED`。
- 性质：打包/K8 重签/启动验证包，不是 DX30/DX45 功能补丁；功能侧仍缺 SIF/TG 运行期映射。

### 54ed5f2c「添加预拍摄」

- 第一版 `Z6_2_0170_preshoot.bin` **FAIL**：状态块写在只读 mas 映射，QEMU 抓到
  `Data Abort on CPU 1, DFSR 0x81d, DFAR 0x1002e280`，启动中断（GPU 0/0、双核空转）。
- 修复版 `Z6_2_0170_preshoot_dram.bin` **PASS**：
  - guest 2.43 s、上传 304、渲染 290、HUD 正常，无任何 Data Abort；
  - `0xC5EFF000`：magic `0x5350365A`、count `0xF6`、retained 48、armed 1、
    补码 `0xACAFC9A5`；
  - `0xC5EFF100` 起描述符为 `{slot=2, arg0/arg1 指针, lr=0x10228BC3}`，冷启动即产生
    ~246 条事件，说明钩子在正常启动路径上确实被调用；
  - 旧状态地址 `0x1002E280` 保持全零，确认运行期不再写镜像区。
- 已知限制：数据面未接（记录事件描述符而非像素）；trigger 槽位仍是假设。
