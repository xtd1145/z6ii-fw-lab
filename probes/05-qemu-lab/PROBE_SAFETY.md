# 05-qemu-lab 探针安全审计（主线）

审计时间：2026-10-29。范围：本目录脚本 + `tools/src/qemu/hw/arm/nikon-z72.c` 里的 `Z62_*` 环境探针。

## 1. 写调用清单与目标

| 文件:行 | 写调用 | 实际目标 | 类别 |
|---|---|---|---|
| `boot_one.py:69` | `Path.write_bytes` | `.local/webui/extracted/<name>.bi` | 宿主副本文件 |
| `direct_containers.py:68` | `Path.write_bytes` | 同上 | 宿主副本文件 |
| `try_containers.py:94` | `Path.write_bytes` | 同上 | 宿主副本文件 |
| `try_containers.py:166` | `Path.write_text` | `.local/webui/variants_try.json` | 宿主结果文件 |
| `direct_containers.py:135` | `Path.write_text` | 同上 | 宿主结果文件 |
| `soak_stock.py:44` | `open(LOCK,"w")` | `.local/webui/soak_stock.lock` | 宿主锁文件 |
| `soak_stock.py:177` | `open(args.out,"w")` | `.local/webui/soak100.json` | 宿主结果文件 |
| `menu_find.py:77` | socket write | HMP/QMP 文本命令 | 网络 |
| `try_variants.py` / `try_containers.py` | HTTP POST | 控制台 `/api/*`（selections/start/stop） | 进程状态 |
| `nikon-z72.c` `Z62_CLEAR_STATUS` | 直接写 RAM host 指针 | 客体 RAM `0xc253e0c0/0xc253e1d0` | 仿真内存 |
| `nikon-z72.c` `Z62_MENUSTRX` | 直接写 RAM host 指针 | 客体 mas 字符串区 `0x110b17f6` | 仿真内存 |
| `nikon-z72.c` `Z62_LWATCH` | `memory_region_add_subregion_overlap` | 客体 RAM 上盖 IO 区间（仅本进程） | 仿真内存 |

**结论：没有任何 NOR 裸扇区、分区表、卡引导扇区或块设备的写入路径。** 所有写都是宿主结果文件
或 QEMU 进程内的客体内存。

## 2. 复现命令

```bash
grep -rn "open(\|write_bytes\|write_text\|\.write(\|memsave\|pmemsave" probes/05-qemu-lab --include=*.py
grep -n "Z62_" tools/src/qemu/hw/arm/nikon-z72.c
```

## 3. 已知坑（不是变砖，但会误导）

- **QMP `pmemsave` 在本 machine 上写出全 0**：必须用 HMP `memsave`（虚拟地址）。
- `-d exec` 只打 TB 链接（宿主指针），PC trace 必须用 `-d in_asm` + `-dfilter`。
- `set_irq_in /machine/controller ...` 会触发 `DEVICE()` 断言并让 **QEMU 进程退出**；
  pad 注入要用 `/machine/unattached/device[22]`（`nikon-z72-controller-config`）。
- `Z62_LWATCH` 在客体 RAM 上盖 IO 区间会让固件报错/启动异常（曾导致 Error 框），
  仅作诊断、默认关闭。
- vvfat（`-drive file=fat:rw:<dir>`）在本机生成 504 MiB 卡，SD 模型要求 2 的幂，
  启动直接失败；不要用它替代真实卡镜像。

## 4. 结论

本目录探针的扇区风险为 **无**。若要改成任何“写卡/WOR 偏移”的操作，必须先走
`probes/SAFETY.md` 的审批规则。
