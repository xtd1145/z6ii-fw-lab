# 真 NOR 参数导入 + live view 缺口定位

日期：2026-11-12
输入：`MT28FW01GABA1L(RB162)@BGA64_20261028_831.BIN`（128.0 MiB）

---

## 1. 这颗 Flash 是主 NOR，参数组齐全

### 尺寸与身份

| 项 | 值 |
|---|---|
| 文件大小 | 134,218,752 B = **128.0 MiB**（比 128 MiB 整数多 1024 B，应是编程器附带的配置/OTP 尾） |
| 熵 | 3.142 bits/byte；0xFF 57.1%、0x00 18.8% |
| `0x000000` | **ARM 向量表**（`18 f0 9f e5` ×7 = `ldr pc,[pc,#-0x18]`） |
| `0x000080` | 版本串 `"0.03"` |
| `0x000094` | 魔数 **`LAPP`**（Loader APP） |
| Nikon 字符串 | 6 处（`0x39c984` 起） |
| NIKON 字符串 | 6 处（`0x23e208` 起） |
| **VR1850** | 2 处（`0x39d238`、`0x209e524`） |
| **SLA** | 2 处（`0x39d253`、`0x2433b06`） |
| ARM 向量表特征 | 全片只有 1 处（`0x0`） |

### 三个参数组全部命中，且都以 ENDM 结尾

| 组 | CPU VA | dump 偏移 | 非 0xFF 占比 | 头部 | 尾部 |
|---|---|---|---|---|---|
| group17 | `0x132c0000` | `0x32c0000` | 72.8% | **`"AUDIO"`**（`41 55 44 49 4f`） | `03 00 00 00 ENDM` |
| group18 | `0x132e0000` | `0x32e0000` | 100.0% | **`"2026568"`** | `9c 00 00 00 ENDM` |
| group32 | `0x13e80000` | `0x3e80000` | 90.2% | 全 0 | `ENDM` |

`nor_import.py` 校验通过：

```
group 17: ok entropy=1.71 nonzero=131072  sha256=6bf5a0330eb353f1
group 18: ok entropy=0.02 nonzero=655360  sha256=2a7c9043b92d2da9
group 32: ok entropy=2.63 nonzero=1572832 sha256=8b3f1bf230c1c913
```

产物：`.local/parameters_real/group{17,18,32}.bin`

**这解决了之前认定的"唯一阻塞项：参数只能从机身导出"。**

---

## 2. 真参数跑 QEMU：画面全黑，但不是卡死

### 现象

| 参数 | 45 s 画面 | GPU uploads | GPU rendered | CPU0 PC |
|---|---|---|---|---|
| 实验室（合成） | nonblack **1364**（有 HUD） | 280 | — | `0x1065600a`（真实代码） |
| **真 NOR** | nonblack **0**（纯黑） | 240 | 217 | `0x1010707c`（**idle**） |

55 s / 75 s 采样：四个核全在 idle（`0x1010707c` / `0x10107094` / `0x10038a68` / `0x10038a50`），
GPU 仍在渲染（217 → 361 → 383）。**固件完成了初始化、进入空闲，但没有画出可见内容。**

### 逐层排查（全部通过，直到最后一环）

| 环节 | 值 | 判定 |
|---|---|---|
| LCD 使能 `0x5200` | `0x00000011` | ✅ bit0 = 1 |
| 几何 `0x4204` | `0x02aa0400` | ✅ w=1024, h=682（偶数、≤2048） |
| video 使能 `0x4110` | `0x00000001` | ✅ |
| 像素格式 `0x4200` | `0x00300000` | ✅ 非 NV12（Y/UV stride 相同 → YUV422） |
| Y 地址 `0x4210` | `0xdcf33000` | ✅ 在 `z62-image-buffer-pool-lab` 内 |
| UV 地址 `0x4220` | `0xdcfdd800` | ✅ = Y + 1024×682，正好接续 |
| Y stride `0x4240` | `0x00000400` | ✅ 1024 |
| UV stride `0x4250` | `0x00000400` | ✅ 1024 |

**所以 `lcd_present` 的 `video` 条件全部满足，会去渲染 video —— 但那块内存是 0。**

### 结论

**真参数让固件进入了真正的取景（live view）模式，显示源是 `0xdcf33000` 的 YUV422 帧。**

而 QEMU 里那块缓冲区从来没被填过，所以画面纯黑。

这也解释了之前的观察：实验室参数下固件走了**降级路径**，反而会画 HUD + Error 框
（那是"出错时也要显示点东西"的行为）；真参数走正常路径，于是暴露了 live view 缺口。

**这正是用户清单里"无 live view、无 LCDC 线引擎"那一条的具体落点，而且现在有真参数可以稳定复现。**

---

## 4. live view 填充：画面已出

### 实现

`z62_sensor_sim_fill_yuv()`（sensor-sim.c）+ LCD 侧调用：

- 复用同一套 `sensor_scene_pixel`（**不是凭空造图案**）
- RGB → BT.601：`Y=(77R+150G+29B)>>8`，色度按像素对平均
- Y 写 `ya`（stride `ys`），U/V 交错写 `ua`（stride `us`）
- LCD 新增 `sensor-sim` link 属性 + `live-view-fill` 开关（**默认 off**，不影响实验室行为）
- 只在 video 分支且首次时填一次

### 结果

```
Z62_LIVEVIEW filled 1024x682 y=dcf33000 uv=dcfdd800 strides=1024/1024
```

| 配置 | 画面颜色种数 | 内容 |
|---|---|---|
| 真参数 + 无填充 | **1** | 纯黑 |
| 真参数 + live view 填充 | **84** | **sensor 色条**（`#ffffff`/`#000000`/`#01fb01`/`#fcfe03`/`#fb0302`） |
| 实验室参数（对照） | 215 | HUD 图形 |

出现的主色正好是 sensor-sim 里 `scene_bars` 的色条 —— **闭环**：
固件配置的显示窗口 → 模型按同一场景函数填 YUV → 面板显示出可识别内容。

55 s 时 cpu0 = `0x10579688`（真实代码，非 idle），说明固件确实在跑显示流程。

---

## 5. 与"第二颗 Flash"的关系

这颗是**主 NOR**（含 MAIN/SLA/VR1850 名字与三个参数组），不是第二介质：

| 判据 | 结论 |
|---|---|
| 含三参数组（ENDM 结尾） | ✅ 主 NOR |
| CPU 视角 64 MiB 内的参数偏移全部命中 | ✅ |
| 全片只有一个 ARM 向量表（`0x0`） | 说明只有一份 loader，符合主 NOR |
| 128 MiB 芯片只用前 ~64 MiB | 容量冗余，正常 |

**"第二颗 Flash"仍未找到** —— 它不在这个 dump 里。

---

## 6. 下一步

1. 参数层已验证可用，之后所有实验默认用 `.local/parameters_real/`。
2. 真参数现在是"正常路径"基线；实验室参数是"降级路径"基线，两者都有用。
3. 相机拍照（CapSt）链可以在真参数下重跑一次 —— 之前所有 capseq 实验用的都是实验室参数。

## 7. 复现命令

```bash
# 导入（已完成）
python nor_import.py "C:/Users/BI/Downloads/MT28FW01GABA1L(RB162)@BGA64_20261028_831.BIN"

# 真参数跑 + PC + LCD 写 trace
Z62_LCD_TRACE=1 python work/qemu_build/board_probe.py --main-screen \
  --at 55,75 --pc --out-dir run/screen/realnor \
  --aux ".local/parameters_real/group17.bin@0x132c0000,,\
.local/parameters_real/group18.bin@0x132e0000,,\
.local/parameters_real/group32.bin@0x13e80000"
```
