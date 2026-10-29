"""Build the integrated Windows-native QEMU 11.1.2 using the local portable SDK."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--source',type=Path,default=ROOT/'tools/src/qemu-11.1.2')
p.add_argument('--build-dir',type=Path)
p.add_argument('--jobs',type=int,default=8)
p.add_argument('--prepare-only',action='store_true')
a=p.parse_args()
source=a.source.resolve();build=(a.build_dir or source/'build-baseline').resolve()
if (source/'VERSION').read_text().strip()!='11.1.2':p.error('Expected original QEMU 11.1.2 source')
if not 1<=a.jobs<=64:p.error('--jobs must be 1..64')
env=os.environ.copy();env['GIT_CEILING_DIRECTORIES']=str(source.parent)
patch=ROOT/'qemu/host-compat.patch'
already=subprocess.run(['git','apply','--reverse','--check',str(patch)],cwd=source,env=env,capture_output=True).returncode==0
if not already:
    subprocess.run(['git','apply','--check',str(patch)],cwd=source,env=env,check=True)
    subprocess.run(['git','apply',str(patch)],cwd=source,env=env,check=True)
for folder in ['hw/arm','system']:
    for path in (ROOT/'qemu'/folder).iterdir():
        if not path.is_file():continue
        target=source/folder/path.name
        if not target.exists() or target.read_bytes()!=path.read_bytes():shutil.copy2(path,target)
config=source/'configs/devices/arm-softmmu/default.mak'
if 'CONFIG_NIKON_Z72_LAB=y' not in config.read_text():
    with config.open('a') as out:out.write('\nCONFIG_NIKON_Z72_LAB=y\n')
if a.prepare_only:
    print('Integrated source overlay and host patch are ready')
    raise SystemExit(0)
if os.name!='nt':p.error('This entry builds Windows; the original Linux project remains in ../z62-qemu')
bash=ROOT/'tools/msys64/usr/bin/bash.exe'
python=ROOT/'tools/python312/python.exe'
if not bash.exists() or not python.exists():p.error('Prepare portable MSYS2 and tools/python312 as described in docs/整合进展-20261029.md')
build.mkdir(parents=True,exist_ok=True)
env.update(MSYSTEM='UCRT64',CHERE_INVOKING='1',TMP=str(ROOT/'tools/msys64/tmp'),TEMP=str(ROOT/'tools/msys64/tmp'))
options=['--target-list=arm-softmmu','--without-default-devices','--disable-docs','--disable-tools','--disable-user','--disable-werror','--disable-gtk','--disable-sdl','--disable-vnc','--disable-opengl','--disable-dbus-display','--disable-guest-agent','--disable-slirp','--disable-rust','--disable-debug-info']
quote=lambda x:shlex.quote(str(x).replace('\\','/'))
steps=['export PATH="/ucrt64/bin:/usr/bin:/c/Program Files/Git/cmd:$PATH"','cd '+quote(build)]
if not (build/'build.ninja').exists():steps+=[' '.join([quote(source/'configure'),'--python='+quote(python),*options])]
else:steps+=['./pyvenv/Scripts/meson.exe setup --reconfigure']
steps+=[f'ninja -j{a.jobs} qemu-system-arm.exe']
subprocess.run([str(bash),'-lc','set -e; '+'; '.join(steps)],cwd=ROOT,env=env,check=True)
runtime=ROOT/'runtime';runtime.mkdir(exist_ok=True)
shutil.copy2(build/'qemu-system-arm.exe',runtime/'qemu-system-arm.exe')
for dll in (ROOT/'tools/msys64/ucrt64/bin').glob('*.dll'):shutil.copy2(dll,runtime/dll.name)
subprocess.run([str(runtime/'qemu-system-arm.exe'),'--version'],check=True)
