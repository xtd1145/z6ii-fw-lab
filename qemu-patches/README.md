# QEMU 改动（Nikon Z6 II 实验室机器）

本目录是我们对上游 QEMU 的全部改动，供在干净源码树上复现。

- 上游版本：**QEMU 11.1.2**
- 内容：
  - `hw/arm/nikon-*.c` / `*.h` —— 机器与设备模型
  - `hw/arm/Kconfig`、`hw/arm/meson.build` —— 注册机器类型与源文件
  - `system/memory.c`、`system/physmem.c` —— 内存/物理地址空间的配套改动

## 应用方式

1. 取 QEMU 11.1.2 源码树。
2. 把本目录的 `hw/` 与 `system/` 覆盖到源码对应位置（`cp -r qemu-patches/hw qemu-patches/system <qemu-src>/`）。
3. 按常规配置构建 `qemu-system-arm`。
4. `meson-entries.txt` 记录了 `hw/arm/meson.build` 里新增的源文件条目，便于核对。

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
