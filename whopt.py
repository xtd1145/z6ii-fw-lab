import numpy as np,sys
raw=open(sys.argv[1],'rb').read()
BASE=0x10000000
lo=int(sys.argv[2],16); hi=int(sys.argv[3],16)
A=np.frombuffer(raw,dtype='<u4',count=len(raw)//4).astype(np.int64)
m=((A>=BASE+lo)&(A<BASE+hi))
idx=np.where(m)[0]
print('pointers into',hex(lo),hex(hi),':',len(idx))
for i in idx:
    o=i*4
    print(f'  ref@{o:#09x}(vma {BASE+o:#x}) -> {int(A[i])-BASE:#x}')
