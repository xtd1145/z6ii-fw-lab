"""只读 USB 快照：记录尼康相机在 Windows 上的全部接口，用于对比正常模式与被拒状态。

不安装驱动、不 open 设备、不发任何 USB 请求；只读 PnP 与注册表。
用法：
  python usb_snapshot.py            # 采集一份，存 work/usb/snap_*.json
  python usb_snapshot.py --diff A B # 对比两份快照
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

NIKON_VID = "04B0"
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work", "usb")

PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rows = @()
Get-CimInstance Win32_PnPEntity | Where-Object {
    $_.PNPDeviceID -like 'USB\*' -or $_.PNPDeviceID -like 'USBSTOR\*' -or $_.PNPDeviceID -like 'SWD\WPDBUSENUM\*'
} | ForEach-Object {
    $rows += [pscustomobject]@{
        pnp     = $_.PNPDeviceID
        name    = $_.Name
        class   = $_.Class
        status  = $_.Status
        problem = $_.ConfigManagerErrorCode
        service = $_.Service
    }
}
$reg = @()
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB' | Where-Object {
    $_.PSChildName -like '__VIDPAT__'
} | ForEach-Object {
    $dev = $_.PSChildName
    Get-ChildItem $_.PSPath | ForEach-Object {
        $p = Get-ItemProperty $_.PSPath
        $reg += [pscustomobject]@{
            device     = $dev
            instance   = $_.PSChildName
            desc       = $p.DeviceDesc
            class      = $p.Class
            service    = $p.Service
            compat     = ($p.CompatibleIDs -join ',')
            container  = $p.ContainerID
            driver     = $p.Driver
            location   = $p.LocationInformation
        }
    }
}
[pscustomobject]@{ devices = $rows; nikon = $reg } | ConvertTo-Json -Depth 6
"""


def collect():
    script = PS.replace("__VIDPAT__", "VID_" + NIKON_VID + "*")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        sys.exit("PowerShell 枚举失败: %s" % exc)
    if not r.stdout.strip():
        sys.exit("PowerShell 无输出: %s" % (r.stderr.strip()[:400] or "空"))
    return json.loads(r.stdout)


def summarize(snap):
    nk = snap.get("nikon") or []
    if isinstance(nk, dict):
        nk = [nk]
    print("尼康 VID_%s 节点: %d" % (NIKON_VID, len(nk)))
    for n in nk:
        print("  %s / %s" % (n.get("device"), n.get("instance")))
        print("    desc=%s class=%s service=%s" % (n.get("desc"), n.get("class"), n.get("service")))
        if n.get("compat"):
            print("    compatible=%s" % n["compat"])
        if n.get("location"):
            print("    location=%s" % n["location"])
    devs = snap.get("devices") or []
    if isinstance(devs, dict):
        devs = [devs]
    usb = [d for d in devs if "VID_04B0" in (d.get("pnp") or "").upper()]
    if usb:
        print("PnP 里属于相机的 devnode:")
        for d in usb:
            print("  %-12s problem=%s %s" % (d.get("class") or "-", d.get("problem"), d.get("name")))
    else:
        print("PnP 里没有相机 devnode（设备未连接或未开机）。")
    return len(nk) + len(usb)


def diff(a_path, b_path):
    a = json.load(open(a_path, encoding="utf-8"))
    b = json.load(open(b_path, encoding="utf-8"))

    def key(s):
        out = set()
        for n in (s.get("nikon") or []):
            if isinstance(n, dict):
                out.add((n.get("device"), n.get("instance"), n.get("compat"), n.get("service")))
        return out

    ka, kb = key(a), key(b)
    print("A: %s (%d 节点)" % (a_path, len(ka)))
    print("B: %s (%d 节点)" % (b_path, len(kb)))
    for tag, items in (("仅 A", ka - kb), ("仅 B", kb - ka)):
        for it in sorted(items):
            print("  %s: %s" % (tag, it))
    if ka == kb:
        print("两份快照的尼康接口集合相同。")


def main():
    ap = argparse.ArgumentParser(description="只读 USB 接口快照（不打开设备、不发送请求）")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"), help="对比两份快照")
    ap.add_argument("--out", help="快照输出路径")
    args = ap.parse_args()

    if args.diff:
        diff(*args.diff)
        return 0

    snap = collect()
    snap["_captured_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    os.makedirs(OUTDIR, exist_ok=True)
    out = args.out or os.path.join(
        OUTDIR, "snap_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=1)
    n = summarize(snap)
    print("快照: %s" % out)
    print("判读: 出现 MI_xx 且 compatible=USB\\Class_FF&SubClass_F0 的节点 → vendor 接口被枚举；"
          "只有 06/01/01 或 WPD → 纯 PTP/MTP。")
    return 0 if n else 3


if __name__ == "__main__":
    sys.exit(main())
