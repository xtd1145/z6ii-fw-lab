# Z6II 实验室 · 全部工作流总表（2026-10-30）

`work/` 下的工作流按用途分三类：**可加载探针**（已汇总到本目录子文件夹）、**固件改造/验证**、
**静态分析**。标记 ✅ 的已收入本目录。

## A. 可加载探针（已汇总）

| 工作流 | 目录 | 探针 | 汇总位置 | 安全审计 |
|---|---|---|---|---|
| 预拍摄 | `work/preshoot/`, `work/preshoot_build/` | 8 站点钩子容器、Unicorn A/B、容器验证 | ✅ `01-preshoot/` | 进行中 |
| 连拍/抓流 | `work/dx30_raw/`, `work/dx45/`, `work/dx45_jpeg/` | P0–P5 只读 dump/trace/监视 | ✅ `02-burst/` | 无风险 |
| ZLOG8 | `work/zlog/`, `work/nlog/`（历史） | NP3 载体、曲线/LUT、unicorn 路径探针 | ✅ `03-zlog/` | 已加固路径守卫 |
| 快门音 | `work/beep_fix/` | 全尺寸签名变体、QEMU 矩阵 | ✅ `04-beep/` | 无风险 |
| QEMU 实验台 | `work/qemu_build/`, `work/menu_re/` | 冷启动/容器/soak/菜单/PC trace | ✅ `05-qemu-lab/` | 无风险 |

## B. 固件改造 / 验证（含产物或规格，未收入探针目录）

| 工作流 | 目录 | 内容 |
|---|---|---|
| K8 签名 | `work/k8/` | `SHA1(body‖K8)` 验证、重签/封包链、诱饵/跨机型测试 |
| 卡载核心格式 | `work/z6rc/` | Z6RC 容器格式（逆向自 SHCORE） |
| 卡载/多进程加载 | `work/wlmech/`, `work/wlport/`, `work/wlz6/` | WonderLantern Z6 机制、移植与规格 |
| 快门解锁 | `work/shutter/` | 字节级证据与可行性结论 |
| 多帧/像素位移 | `work/multiexp/` | 有效位深、叠加/位移边界 |
| Open Gate | `work/opengate/` | 4K30 Open Gate 补丁工作单 |
| 视频→JPEG | `work/video2jpeg/` | 4K60 H.264 → DX60 JPEG 改造分析 |
| 肩屏 | `work/toplcd/` | 肩屏显示逆向 |
| 间隔拍摄 | `work/interval/` | 间隔/定时相关引用索引 |
| AF-C 提示 | `work/afc/` | 合焦提示静态定位 |
| BVR | `work/bvr_feasibility/` | BVR 协议 + VR 模型可行性 |
| USB | `work/usb/` | USB 抓包与诱饵包构造 |
| 仿真验证 | `work/emu/`, `work/z6ii_mem/`, `work/z6ii_pool/`, `work/z6ii_hooks/` | 指针统计、页映射 Thumb 模拟器、基址/池/钩子验证 |

## C. 静态分析工具（不加载到相机）

| 工作流 | 目录 | 内容 |
|---|---|---|
| 定位/常量扫描 | `work/loc/` | adr/常量/指针/区间扫描 |
| 颜色 | `work/color/` | FAT32 读写工具、测试图 |
| 补丁 dry-run | `work/patch_dry/` | 补丁预演 |
| 上游源码 | `work/upstream_tmp/` | 上游 QEMU 仓库临时副本 |

## 加载探针的统一定义

“可加载探针”= 需要进入相机/QEMU 目标环境执行的代码或数据：
① 签名容器（刷机/升级）；② 卡上文件（NP3、探针包）；③ 运行期内存注入/监视；
④ QEMU 侧 hook/env 探针。纯离线反汇编/统计脚本不算。

## 结论

- 全部 5 类可加载探针已集中到本目录，每类都有代码 + README + 写目标说明。
- 扇区安全审计：4 类已完成（1 类进行中），唯一真实风险（ZLOG 任意输出路径）已修复并加守卫。
