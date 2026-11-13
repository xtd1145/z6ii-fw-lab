# 05 · 主线 QEMU 实验台探针

## 用途

单包/全变体冷启动、容器解包、soak、菜单查找，以及实验台的环境变量探针。
所有写入都在 QEMU 进程内存/宿主结果文件里。

## 文件

- `boot_one.py PATH [--tag NAME]` — 单包冷启动（容器自动解包），判据：guest ≥2.4 s、
  上传 ≥250、渲染 ≥200、HUD >30、cpu0 ≠ `0x101034fc`。
- `try_variants.py` — 遍历 `dist/` 全部变体（按 sha 分组），各跑一遍并写
  `.local/webui/variants_try.json`；每项结束恢复原厂 master。
- `try_containers.py` / `direct_containers.py` — 容器离线解包 + 只启动（不依赖 HTTP 控制台）。
- `soak_stock.py --repeats N` — 启动稳定性 soak（100 遍基线：100/100 PASS）。
- `menu_find.py` — 启动板子后枚举 GPIO/boot-config，用 RSP 搜菜单指针（实验性）。

## 环境变量探针（改 `hw/arm/nikon-z72.c` 或运行时注入）

| 变量 | 作用 |
|---|---|
| `Z62_CLEAR_STATUS=1` | 客体 1.3 s 清空参数 0x1921 状态列表（解锁菜单、去掉 Error 框） |
| `Z62_MENUSCAN=1` | 4.5 s 扫描菜单记录/引用，日志 `z62.menurec/menuref` |
| `Z62_MENUDUMP=1` | dump 记录周边内存（地址已修正到 runtime heap） |
| `Z62_MENUSTRX=1` | 把 mas 里 "Slide show" 文案原地改为 "PATCHES"（文案通路验证） |
| `Z62_WLOG=1` | 记录 transport/physmem 的 0x44 写入（早期追状态源用） |
| `Z62_LWATCH=1` | 运行期范围写监视（注意：覆盖 RAM 的 IO 区间会让固件报错，慎用） |
| `Z62_TLOG=1` | 记录**全部** transport 写（偏移/值/大小/cpu/pc，上限 800） |
| `Z62_MSGLOG=1` | 每 20 ms(客体) diff 共享 message RAM 前 8 KiB，记录变化字（查按键/ExMCU 报文） |


## 仪表口径（重要）

- **dump 用 HMP `memsave`（虚拟地址）**；QMP `pmemsave` 在这台 machine 上写出全 0，不可用。
- **PC trace 用 `-d in_asm` + `-dfilter`**（`-d exec` 只打 TB 链接，没有客户机 PC）。
- 按键：`/api/key`（menu=9、ok=3、up=5、down=4、left=7、right=6），hold 以墙钟计，
  建议 ≥1.2 s。
- 外设 pad：`set_irq_in /machine/unattached/device[22] unnamed-gpio-in <port*16+pin> 0|1`
  （device[22] = `nikon-z72-controller-config`；**不要**打 `/machine/controller`，
  它是容器对象，`DEVICE()` 断言会让 QEMU 直接退出）。

## 写入目标

- QEMU 进程 RAM（断电即消失）；
- `.local/webui/**` 的结果/dump/选择记录；
- 不会写 NOR、固件镜像或卡内容（卡以 `snapshot=on` 挂载，写入不落盘）。

**扇区风险：无。**

## 回滚

停止 QEMU；删除 `.local/webui/` 下的 dump/结果即可。
