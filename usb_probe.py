"""枚举 USB 设备，找尼康 Z6 II 上的额外接口 / CDC-ACM / vendor 通道（只读，不发送任何厂商操作）。

优先用 pyusb 读接口/端点；没有 pyusb（或 libusb 不可用）时退回 PowerShell 的 PnP 视图。
"""
import re
import subprocess
import sys

NIKON_VID = 0x04B0


def show_itf(itf):
    parts = [f"  itf {itf.bInterfaceNumber}.{itf.bAlternateSetting}",
             f"class={itf.bInterfaceClass:#04x}/{itf.bInterfaceSubClass:#04x}/{itf.bInterfaceProtocol:#04x}"]
    eps = [f"ep{ep.bEndpointAddress:#04x}{'IN' if ep.bEndpointAddress & 0x80 else 'OUT'}/{ep.wMaxPacketSize}"
           for ep in itf]
    print(" ".join(parts), " ".join(eps))


def try_pyusb():
    try:
        import usb.core
    except ImportError:
        return None
    dev = usb.core.find(idVendor=NIKON_VID)
    if dev is None:
        return "no-device"
    print(f"Nikon device: idVendor=0x04b0 idProduct={dev.idProduct:#06x} bus={dev.bus} addr={dev.address}")
    try:
        cfg = dev.get_active_configuration()
    except Exception as exc:
        print(f"读活动配置失败（驱动占用）: {exc}")
        print("→ 用 Zadig 把该接口替换为 WinUSB/libusb 后重试（只换驱动，不写设备）")
        return "busy"
    print(f"active config {cfg.bConfigurationValue}, {len(cfg)} interfaces:")
    for itf in cfg:
        show_itf(itf)
    return "ok"


def powershell_fallback():
    ps = (
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.PNPDeviceID -match '^USB' } | "
        "ForEach-Object { '{0}|{1}|{2}' -f $_.PNPDeviceID, $_.Class, $_.Name }"
    )
    try:
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=90).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"PowerShell 枚举失败: {exc}")
        return 1
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    vids = {}
    nikon = []
    for line in lines:
        devid, cls, _, name = (line.split("|") + ["", ""])[:4]
        m = re.search(r"VID_([0-9A-Fa-f]{4})", devid, re.I)
        if m:
            vids.setdefault(m.group(1).upper(), set()).add(name or devid)
        if m and m.group(1).upper() == "04B0":
            nikon.append((devid, cls, name))
    if nikon:
        print(f"Nikon (VID_04B0) 节点 {len(nikon)} 个：")
        for devid, cls, name in nikon:
            print(f"  class={cls or '-':12s} {name}\n    {devid}")
        print("\n注意：PnP 视图给出的是 Windows 绑定的设备接口，看不到复合设备里未被绑定的"
              " vendor/CDC 接口；要用 pyusb+libusb 才能列出全部 interface。")
    else:
        print("PnP 里没有 VID_04B0（尼康）节点。当前在线的厂商 VID：")
        for vid, names in sorted(vids.items()):
            if vid in ("0000",):
                continue
            sample = next(iter(names))[:60]
            print(f"  VID_{vid}  {sample}")
        print("→ 把相机用数据线接上、开机、USB 模式设为 MTP/PTP 后重跑本脚本。")
    return 0


def main():
    r = try_pyusb()
    if r in ("ok", "no-device"):
        print("\n判读：")
        print("  只有 class=06/01 (PTP) 或 06/01/01 -> 纯 PTP/MTP，没有调试口，USB 读不了内存")
        print("  出现 class=02 (CDC-ACM) 或 class=ff (vendor) 且带 IN 端点 -> 可能是 logoutputchg 的 USB 通道")
        if r == "no-device":
            print("没找到 Nikon 设备 (idVendor 0x04b0)，改用 PnP 视图确认：")
            return powershell_fallback()
        return 0
    return powershell_fallback()


if __name__ == "__main__":
    sys.exit(main())
