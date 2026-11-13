# QEMU 改动（NikON Z6 II 实验室机器）

本目录是我们对上游 QEMU 的全部改动，供在干净源码树上复现。

- 上游版本：**QEMU 11.1.2**
- 改动范围：`hw/arm/nikon-*.c` / `*.h`（36 个文件）

## 应用方式

1. 取 QEMU 11.1.2 源码树。
2. 把 `hw/arm/` 下的 `nikon-*` 文件拷到源码的 `hw/arm/`。
3. 在 `hw/arm/meson.build` 的 `arm_ss` 源文件列表里加入这些条目（见 `meson-entries.txt`）。
4. 按常规配置构建 `qemu-system-arm`。

## 机器类型

| 类型名 | 说明 |
|---|---|
| `nikon-z72-dual-lab` | 双引擎（MAS/SLA）实验室机器，共享 message RAM |
| `nikon-z62-dual-lab` | 在上一行基础上加 Z62 专属设备（LCD、sensor-sim、sensor-config、CVP） |

单角色变体由 `nikon-z72` / `nikon-z72-slave` 提供。

## 注意

- 这些文件是从工作副本直接拷出的当前状态，不是相对上游的 diff。
- `fw_verified/eg1850_*` 等固件镜像不在仓库内（见 `.gitignore`），需要自备。
- 实验开关（`laboratory-*` 属性）默认全部关闭，默认行为与上游建模一致。
