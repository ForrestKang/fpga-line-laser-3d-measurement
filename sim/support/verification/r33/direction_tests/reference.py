"""R33 integer direction and ridge-run oracle, independent of RTL execution."""
from pathlib import Path
import importlib.util
import numpy as np

def direction(gx, gy, xx, xy, yy):
    nx, ny = (xx, xy) if abs(xx) >= abs(yy) else (xy, yy)
    px, py = xx*nx+xy*ny, xy*nx+yy*ny
    shift=max(0, max(abs(px), abs(py)).bit_length()-10)
    nx, ny=px >> shift, py >> shift
    gn=gx*nx+gy*ny
    den=nx*(xx*nx+xy*ny)+ny*(xy*nx+yy*ny)
    ax,ay=-gn*nx,-gn*ny
    valid=den<0 and 2*abs(ax)<=abs(den) and 2*abs(ay)<=abs(den)
    if not valid:return False,0,0,nx,ny
    def divide(n):return (-1 if (n<0)!=(den<0) else 1)*((abs(n)<<12)//abs(den))
    return True,divide(ax),divide(ay),nx,ny

def candidates(image):
    path=Path(__file__).parents[1]/'window_tests/reference.py'
    spec=importlib.util.spec_from_file_location('window_reference',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    image=np.asarray(image,dtype=np.uint8); fields=module.features(image)
    h,w=image.shape; records=[]; rows=[]; ids=0; qualified=0
    for y in range(h):
        strength=np.maximum(np.maximum(-fields[y,:,3],-fields[y,:,5]),0)
        hit=strength>=24
        if y in (0,h-1):hit[:]=False
        hit[0]=hit[-1]=False
        edges=np.diff(np.r_[False,hit,False].astype(np.int8))
        starts=np.flatnonzero(edges==1);ends=np.flatnonzero(edges==-1)-1
        local=0
        for first,last in zip(starts,ends):
            first,last=int(first),int(last)
            best=first+int(np.argmax(strength[first:last+1]));s=int(strength[best]);f=list(map(int,fields[y,best]))
            valid,du,dv,nx,ny=direction(f[1],f[2],f[3],f[4],f[5])
            # Match deterministic plateau-descent multimodal separation.
            prev=int(strength[first]);rising=True;plateau=first
            have=False;retained=0;retained_x=0;valley=prev;npeaks=0
            for x in range(first+1,last+2):
                cur=int(strength[x]);valley=min(valley,prev)
                if cur<prev and rising and prev>=48:
                    if not have:npeaks=1;have=True;retained=prev;retained_x=plateau;valley=prev
                    elif plateau>=retained_x+3 and valley+16<=min(retained,prev):
                        npeaks+=1;retained=prev;retained_x=plateau;valley=prev
                    elif prev>retained:retained=prev;retained_x=plateau;valley=prev
                if x<=last and cur!=prev:rising=cur>prev;plateau=x
                prev=cur
            sat=int(np.count_nonzero(fields[y,first:last+1,6]))
            reason=(1 if s<48 else 0)|(2 if last-first+1>17 else 0)|(
                4 if first<2 or last>w-3 or y<1 or y>h-2 else 0)|(
                8 if sat else 0)|(16 if npeaks>1 else 0)|(32 if not valid else 0)
            word=[0]*16
            word[:6]=[ids,y,first,last,best*4096+du,y*4096+dv]
            word[6]=((f[2]&4095)<<12)|(f[1]&4095)
            word[7]=((ny&4095)<<12)|(nx&4095)
            word[8]=int(image[y,first:last+1].max());word[9]=sat
            word[10]=int(strength[first:last+1].sum());word[11]=s;word[12]=min(s*1024,1048575)
            mode=6 if reason==0 and local>=16 else 1
            word[13]=(int(valid)<<24)|(mode<<16)|(mode<<8)|reason
            word[14]=0xffffffff;word[15]=npeaks<<24
            if not reason:local+=1;qualified+=1
            ids+=1;records.append(word)
        rows.append({'qualified':local,'raw':len(starts)})
    return records,rows

def graph_patches(records, rows):
    """Independent row-anchored graph, same explicit R31 adjacency contract."""
    nodes={};row_nodes={};overflow=any(r['qualified']>16 for r in rows)
    for rec in records:
        if rec[13]&127:continue
        y=rec[1];slot=len(row_nodes.setdefault(y,[]))
        if slot>=16:continue
        node=y*16+slot;nodes[node]=rec;row_nodes[y].append(node)
    pred={n:[] for n in nodes};succ={n:[] for n in nodes};edges=[]
    for y in range(1,len(rows)):
        if rows[y-1]['qualified']>16 or rows[y]['qualified']>16:continue
        for a in row_nodes.get(y-1,[]):
            for b in row_nodes.get(y,[]):
                ra,rb=nodes[a],nodes[b]
                if abs(ra[4]-rb[4])<=24576 and abs((ra[3]-ra[2])-(rb[3]-rb[2]))<=8:
                    pred[b].append(a);succ[a].append(b);edges.append((a,b))
    amb=set()
    for a,b in edges:
        if len(succ[a])!=1 or len(pred[b])!=1:amb.update((a,b))
    stability={n:0 for n in nodes}
    for a,b in edges:
        if a not in amb and b not in amb:
            d=abs(nodes[a][4]-nodes[b][4]);stability[a]=max(stability[a],d);stability[b]=max(stability[b],d)
    labels={};support={}
    for n in sorted(nodes):
        if rows[n//16]['qualified']>16 or n in amb:continue
        parent=pred[n][0] if len(pred[n])==1 else None
        root=labels.get(parent,n) if parent not in amb else n
        labels[n]=root;support[root]=support.get(root,0)+1
    patches=[]
    for n,rec in sorted(nodes.items()):
        eligible=rows[n//16]['qualified']<=16
        sup=support.get(labels.get(n),0)
        cp=eligible and n not in amb and not overflow and sup>=3
        cm=cp and sup>=8 and rec[12]>=73728
        rm=rp=0
        if overflow:rm=rp=6
        elif n in amb:rm=rp=2
        else:
            if sup<3:rp=3
            if sup<8:rm=3
            elif rec[12]<73728:rm=5
        patch=[0]*16;patch[0]=rec[0]
        patch[13]=(int(cp)<<26)|(int(cm)<<25)|(1<<24)|(rp<<16)|(rm<<8)
        label=labels.get(n,8191)
        patch[14]=((label if cp else 65535)<<16)|(label if cm else 65535)
        patch[15]=stability[n]
        patches.append(patch)
    return patches


def final_rows(image, records, rows, patches):
    """Independent complete row summaries, including final graph acceptance."""
    by_y = {}
    for rec in records:
        if not rec[13] & 127:
            group = by_y.setdefault(rec[1], [])
            if len(group) < 16:
                group.append(rec)
    pred = {}; succ = {}; edges = []
    for y in range(1, len(rows)):
        if rows[y-1]['qualified'] > 16 or rows[y]['qualified'] > 16:
            continue
        for a in by_y.get(y-1, []):
            for b in by_y.get(y, []):
                if abs(a[4]-b[4]) <= 24576 and abs((a[3]-a[2])-(b[3]-b[2])) <= 8:
                    succ[a[0]] = succ.get(a[0], 0) + 1
                    pred[b[0]] = pred.get(b[0], 0) + 1
                    edges.append((a, b))
    ambiguous_y = set()
    for a, b in edges:
        if succ[a[0]] != 1 or pred[b[0]] != 1:
            ambiguous_y.update((a[1], b[1]))
    patch_by_id = {p[0]: p for p in patches}
    output = []
    for y, data in enumerate(rows):
        m = []; p = []
        for rec in by_y.get(y, []):
            bits = patch_by_id[rec[0]][13]
            if bits & (1 << 25): m.append(rec)
            if bits & (1 << 26): p.append(rec)
        output.append([y, int(image[y].max()), int((image[y] >= 250).sum()),
                       int((image[y] == 255).sum()), data['raw'], data['qualified'],
                       len(m), len(p), max((r[3]-r[2]+1 for r in m), default=0),
                       max((r[3]-r[2]+1 for r in p), default=0),
                       max((r[8] for r in m), default=0), max((r[8] for r in p), default=0),
                       int(y in ambiguous_y), int(y in ambiguous_y), 0, 0])
    return output
