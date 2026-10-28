# z6ii-fw-lab

尼康 Z6 II（EG1850 固件）系统级 QEMU 实验室：双核 A15 + Cortex-M4 的 machine 建模、
浏览器实验台（导入固件 / 按键 / 抓屏 / 变体），以及补丁和启动稳定性脚本。

**本仓库不包含任何尼康固件、NOR 转储或 SD 卡镜像**，只包含 QEMU 侧代码与实验脚本。

## 目录

- `qemu/hw/arm/` — 自研 machine `nikon-z6ii` 与 `nikon-z62-dual-lab` 及设备模型，
  按 QEMU 11.1.2 的 `hw/arm` 目录组织（GPL-2.0-or-later）；`Kconfig` / `meson.build`
  是对应版本的注册文件。
- `qemu/system/` — `physmem.c` / `memory.c` 的实验钩子（`Z62_WLOG` 等，默认关闭）。
- `z62_webui/` — 标准库实现的本地实验台：`python z62_webui/console.py --port 8642`。
- `work/` — soak 与补丁分析脚本；`docs/` — 整合方案与说明。
- `work/soak_stock.py` — 启动稳定性测试：每遍等客体时间 ≥ 2.4 s，判定 GPU 上传 ≥ 250、
  渲染命令 ≥ 200、画面非黑采样 > 50、cpu0 不在空转地址。

## 构建

1. 取 QEMU 11.1.2 源码树（arm-softmmu 即可）。
2. 把本仓库 `qemu/hw/arm/` 下的 `nikon-*` 文件拷入源码树 `hw/arm/`，用仓库内的
   `Kconfig`、`meson.build` 覆盖同名文件；`qemu/system/` 的两个文件同理覆盖。
3. 配置并编译：

```
mkdir build && cd build
../configure --target-list=arm-softmmu --disable-werror
ninja qemu-system-arm.exe
```

## 运行

```
python z62_webui/console.py --port 8642
```

浏览器打开 `http://127.0.0.1:8642`，导入 mas/sla/controller 镜像后启动。
实验室参数组与 NOR 属于本地内容，不进仓库。

## 状态

- stock C1.70：可启动到取景 HUD、打开相机菜单、方向键与 OK 子菜单可用。
- 启动稳定性：100 遍冷启动 soak **100/100 通过**（见 `SOAK.md` 与 `results/soak100.json`）。
- 菜单模型：文案实时取自 mas 的 UTF-16 字符串区；运行期菜单在打开时整体重建。
- 仅用于自有设备的互操作与固件研究实验。
