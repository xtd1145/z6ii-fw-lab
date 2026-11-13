import numpy as np,sys
BASE=0x10000000
def bl_targets(path,base=BASE):
    D=np.frombuffer(open(path,'rb').read(),dtype=np.uint16).astype(np.int64)
    a=D[:-1]; b=D[1:]; idx=np.arange(len(a))
    m=((a&0xF800)==0xF000)&((b&0xD400)==0xD000)
    p=idx[m]; aa=a[m]; bb=b[m]
    S=(aa>>10)&1; imm10=aa&0x3FF
    J1=(bb>>13)&1; J2=(bb>>11)&1; imm11=bb&0x7FF
    imm=(S<<23)|((1-J1)<<22)|((1-J2)<<21)|(imm10<<11)|imm11
    imm=np.where(S==1, imm-(1<<24), imm)
    pc=base+p*2+4; tgt=pc+imm*2
    out={}
    for t,s in zip(tgt.tolist(),p.tolist()): out.setdefault(int(t),[]).append(base+s*2)
    return out
if __name__=='__main__':
    res=bl_targets('all_modules/Z6_2_0170/eg1850_mas_01700')
    print('total distinct targets',len(res))
    for t,name in [(0x10391142,'set_key'),(0x10391128,'set_func'),(0x10391136,'set_digest'),
                   (0x1039112e,'set_data'),(0x10391120,'ctx_reset'),(0x10392286,'verify'),
                   (0x1039ea3c,'transform1')]:
        print(name,hex(t),'->',[hex(x) for x in sorted(res.get(t,[]))])
