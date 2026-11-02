/* R20 bounded integer wire admission and fixed-policy fallback selection.
 * No device I/O, state, frame cache, threads, floating point or global buffers.
 * Compile: cc -O3 -std=c99 -shared -fPIC cpu_fast_r20.c -o cpu_fast_r20.so
 */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif

static uint32_t word(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24);
}
static uint32_t rowword(const uint8_t *p, int y, int i) { return word(p+128+24*y+4*i); }
static int cmp_u32(const void *a, const void *b) {
    uint32_t x=*(const uint32_t *)a, y=*(const uint32_t *)b;
    return (x>y)-(x<y);
}
static uint32_t median2(uint32_t *p, int n) {
    if (!n) return 0;
    qsort(p,(size_t)n,sizeof(*p),cmp_u32);
    return p[(n-1)/2]+p[n/2];
}

API int r20_validate(const uint8_t *p, size_t bytes, int fw, int fh) {
    uint32_t h[32], raw[480], peaks[480], widths[480];
    uint32_t valid_n=0, multi_n=0, bright_n=0, clean_n=0, max_width=0;
    uint32_t x0,x1,y0,y1,lo,hi,n; int fixed, fallback=0;
    if (bytes<152 || bytes>11648 || (bytes-128)%24) return 1;
    for (int i=0;i<32;i++) h[i]=word(p+4*i);
    n=h[10];
    if (h[0]!=0x534c4334U || h[1]!=0x10000 || h[2]!=(bytes/4) ||
        n<1 || n>480 || h[2]!=32+6*n || h[30]!=0x55524331U || h[31]) return 2;
    if (fw<1 || fw>752 || fh<1 || fh>480 || h[11]!=(uint32_t)fw || h[12]!=(uint32_t)fh) return 3;
    if (h[4]>>16 || h[9]>>16 || h[13]>>8 || h[14]>>20 || h[15]>>20 ||
        h[16]>>20 || h[17]>>18 || h[18]!=(0xff00U|(h[18]&255)) || h[19]!=0x4e4c5131U) return 4;
    x0=h[14]&1023; x1=h[14]>>10; y0=h[15]&1023; y1=h[15]>>10;
    lo=h[16]&1023; hi=h[16]>>10;
    if (x0>=x1 || x1>(uint32_t)fw || y0>=y1 || y1>(uint32_t)fh ||
        n!=y1-y0 || h[20]!=n || lo<1 || lo>hi || hi>1023) return 5;
    fixed=h[13]==60 && lo==1 && hi==32 && h[17]==30 && (h[18]&255)==15;
    for (uint32_t i=0;i<n;i++) {
        uint32_t a=rowword(p,i,0),b=rowword(p,i,1),c=rowword(p,i,2),d=rowword(p,i,3);
        uint32_t u=rowword(p,i,4),v=rowword(p,i,5);
        uint32_t center=a&0x3fffff,flags=(a>>22)&31,valid=(a>>27)&1,back=(a>>28)&1,fault=(a>>29)&1;
        uint32_t first=b&1023,last=(b>>10)&1023,count=(b>>20)&1023;
        uint32_t width=c&1023,rawpeak=(c>>10)&255,peak=(c>>18)&255,sat=(c>>26)&1,ridge=(c>>27)&7;
        int single=count==1 && !fault, positive=count>0;
        if (a>>30 || b>>30 || c>>30) return 6;
        if ((!single || !(ridge&1)) && ridge) return 7;
        if (back!=(uint32_t)(count>1 || fault) || (!single && (center || flags))) return 8;
        if (!positive && (first || last || width || peak || sat || valid || rawpeak>h[13])) return 8;
        if (positive && (first<x0 || last>=x1 || first>last || width!=last-first+1 ||
            count>(width+1)/2 || peak==0 || rawpeak<=h[13] || peak>255-h[13] || peak!=rawpeak-h[13])) return 8;
        if (positive && sat!=(uint32_t)(rawpeak==255)) return 8;
        if (single && (center<(first<<12) || center>(last<<12))) return 8;
        uint32_t expected=(uint32_t)(width<lo || width>hi) | (sat<<2) | ((uint32_t)(first==x0 || last==x1-1)<<3);
        if ((single && ((flags&13)!=expected)) ||
            (single && peak<((h[18]&255)?h[18]&255:1) && !(flags&2)) ||
            valid!=(uint32_t)(single && !(flags&19))) return 9;
        raw[i]=rawpeak;
        if (valid) { peaks[valid_n]=peak; widths[valid_n]=width; valid_n++; if(width>max_width) max_width=width; }
        multi_n+=count>1; bright_n+=rawpeak>=250; clean_n+=single && !back && !flags;
        if (d>>8 || u>>31) return 10;
        uint32_t sc=u&0x3fffff,sf=(u>>22)&31,sr=(u>>27)&7,present=(u>>30)&1;
        uint32_t ss=v&1023,se=(v>>10)&1023,cl=(v>>20)&31,eligible=(v>>25)&15;
        uint32_t sat_comp=(v>>29)&1,overflow=(v>>30)&1,supported=(v>>31)&1;
        uint32_t es=fixed && !fault && !overflow && (count<=1 || (eligible==1 && !sat_comp));
        if (overflow!=(uint32_t)(count>8) || supported!=es || present!=(uint32_t)(es && count>0)) return 11;
        uint32_t capped=count<15?count:15;
        if (eligible>capped || (!cl && eligible!=capped) || (eligible==count && cl) ||
            (sat_comp && (!(cl&4) || eligible>=count)) || (!count && (cl || eligible || sat_comp))) return 11;
        if (single && (cl!=flags || eligible!=(uint32_t)(flags==0) || sat_comp!=(uint32_t)(flags==4))) return 11;
        if (!present && (sc || sf || sr || ss || se)) return 11;
        if (present && (ss<x0 || se>=x1 || ss>se || sc<(ss<<12) || sc>(se<<12) || (!(sr&1) && sr))) return 11;
        if (single && present && (sc!=center || sf!=flags || sr!=ridge || ss!=first || se!=last)) return 11;
        if (present && count>1 && (sf || ss<first || se>last || se-ss+1>32 || ss==x0 || se==x1-1)) return 11;
        fallback |= (d&128)!=0;
    }
    if (h[8]!=valid_n || h[23]!=multi_n || h[22]!=bright_n || h[21]<bright_n ||
        h[21]>bright_n*(x1-x0) || h[24]!=median2(raw,n) || h[25]!=median2(peaks,valid_n) ||
        h[26]!=median2(widths,valid_n) || h[27]!=max_width || h[29]!=clean_n) return 12;
    uint32_t status=(h[21]*100>n*(x1-x0) || h[22]*20>n)?1:
        (h[23]*20>n || h[27]>32)?2:(!h[8] && h[24]<120)?3:
        (h[8]*5<n*4)?4:(h[25]<120)?3:0;
    if(h[28]!=status) return 13;
    if(h[9]) return 14;
    if(fallback) {
        for(uint32_t i=0;i<n;i++) if(rowword(p,i,3)!=128) return 15;
        return 0;
    }
    if(!fixed) return 16;
    for(uint32_t i=0;i<n;i++) {
        uint32_t d=rowword(p,i,3),u=rowword(p,i,4),v=rowword(p,i,5);
        uint32_t reason=(d>>2)&15,sf=(u>>22)&31,sr=(u>>27)&7;
        if(!(v>>31)) return 16;
        if(!(d&64) || reason>11 || (d&1)!=(uint32_t)(reason==0) ||
            ((d>>1)&1)!=(uint32_t)(reason==0 || reason>=8)) return 17;
        uint32_t expected=0;
        if(sf&2) expected=6; if(sf&1) expected=5; if(sf&8) expected=4;
        if(sf&4) expected=3; if(sf&16) expected=2; if(!(u&(1U<<30))) expected=1;
        if(expected && reason!=expected) return 18;
        uint32_t rr=(sr&4)?0:10;
        if(!(sr&2)) rr=9; if(!(sr&1)) rr=8;
        if(!expected && !(reason==7 || reason==rr || (!rr && reason==11))) return 19;
    }
    return 0;
}

/* Reasons 0..11 match SEGMENT_REASONS; 12..14 are PS row decisions. */
static uint8_t class_reason(int f) {
    if(f&16) return 2; if(f&4) return 3; if(f&8) return 4; if(f&1) return 5; return 6;
}
typedef struct { int64_t center; int start,end,flags; } Run;
static int extract(const uint8_t *g, int x0,int x1,Run *runs) {
    int n=0,x=x0;
    while(x<x1) {
        if(g[x]<=60) { x++; continue; }
        int s=x,peak=0,sat=0; int64_t s0=0,s1=0;
        while(x<x1 && g[x]>60) {
            int weight=g[x]-60; s0+=weight; s1+=(int64_t)weight*x;
            if(weight>peak) peak=weight; sat+=g[x]==255; x++;
        }
        int e=x-1,w=x-s; int64_t center=(s1<<12)/(s0?s0:1);
        int f=(w<1 || w>32) | ((s0<30 || peak<15)<<1) | ((sat>0)<<2) |
              ((s==x0 || e==x1-1)<<3) | ((s0>262143 || s1>134217727 || center>4194303)<<4);
        runs[n].start=s; runs[n].end=e; runs[n].center=center; runs[n].flags=f; n++;
    }
    return n;
}
static uint8_t ridge_reason(const uint8_t *g,int s,int e,int x0,int x1) {
    if(s-11<x0 || e+11>=x1) return 8;
    int peak=0,inner=0,left=0,right=0;
    for(int x=s;x<=e;x++) if(g[x]>peak) peak=g[x];
    for(int x=s+1;x<e;x++) if(g[x]>inner) inner=g[x];
    for(int x=s-11;x<s-3;x++) left+=g[x];
    for(int x=e+4;x<e+12;x++) right+=g[x];
    if(8*peak-(left>right?left:right)<160) return 9;
    if(e-s+1<3 || peak-g[s]<8 || peak-g[e]<8 || inner!=peak) return 10;
    return 0;
}

API int r20_select(const uint8_t *p,size_t bytes,const uint8_t *gray,int fw,int fh,
                  int64_t *centers,int64_t *starts,int64_t *ends,
                  uint8_t *accepted,uint8_t *retained,uint8_t *primary,uint8_t *reason,int32_t *stats) {
    if(bytes<152 || bytes>11648 || (bytes-128)%24) return 1;
    int x0=word(p+14*4)&1023,x1=word(p+14*4)>>10;
    int y0=word(p+15*4)&1023,y1=word(p+15*4)>>10,n=y1-y0;
    uint8_t chosen[480]={0},anchor[480]={0},ambiguous[480]={0};
    int segment[480],sizes[480]={0},low[480],high[480]={0};
    Run runs[376]; int segments=0,selected=0,pl_rows=0,raw_rows=0;
    if(fw<1 || fw>752 || fh<1 || fh>480 || x0>=x1 || x1>fw || y0>=y1 || y1>fh ||
       (size_t)n!=(bytes-128)/24 || word(p+11*4)!=(uint32_t)fw || word(p+12*4)!=(uint32_t)fh) return 1;
    for(int y=0;y<fh;y++) { centers[y]=0; starts[y]=ends[y]=-1; accepted[y]=retained[y]=primary[y]=0; reason[y]=15; }
    for(int i=0;i<n;i++) {
        int y=y0+i; uint32_t a=rowword(p,i,0),b=rowword(p,i,1);
        int count=(b>>20)&1023,flags=(a>>22)&31,back=(a>>28)&1;
        segment[i]=-1; reason[y]=0;
        centers[y]=a&0x3fffff; starts[y]=b&1023; ends[y]=(b>>10)&1023;
        primary[y]=!back; pl_rows+=!back; raw_rows+=back;
        if(!back) {
            chosen[i]=count==1 && !flags;
            if(!count) reason[y]=1;
            if(count==1 && flags) reason[y]=class_reason(flags);
        } else {
            int nr=extract(gray+(size_t)y*fw,x0,x1,runs),eligible=0,sat_comp=0,all=0,unique=-1;
            for(int r=0;r<nr;r++) { int f=runs[r].flags; all|=f; if(!f) {eligible++;unique=r;} sat_comp+=f==4; }
            if(!nr) reason[y]=1;
            else if(nr>8) reason[y]=12;
            else if(!eligible) reason[y]=class_reason(all);
            else if(sat_comp) reason[y]=13;
            else if(eligible>1) { reason[y]=14; ambiguous[i]=1; }
            else { chosen[i]=1; centers[y]=runs[unique].center; starts[y]=runs[unique].start; ends[y]=runs[unique].end; }
        }
        anchor[i]=chosen[i];
    }
    for(int i=1;i<n-1;i++) if(ambiguous[i] && anchor[i-1] && anchor[i+1]) {
        int y=y0+i,nr=extract(gray+(size_t)y*fw,x0,x1,runs),match=0,which=-1;
        for(int r=0;r<nr;r++) if(!runs[r].flags && llabs(runs[r].center-centers[y-1])<=12288 &&
                                  llabs(runs[r].center-centers[y+1])<=12288) { match++;which=r; }
        if(match==1) { chosen[i]=1; centers[y]=runs[which].center; starts[y]=runs[which].start;
                      ends[y]=runs[which].end; reason[y]=0; }
    }
    int previous=-2;
    for(int i=0;i<n;i++) if(chosen[i]) {
        int y=y0+i;
        if(previous!=i-1 || llabs(centers[y]-centers[y0+previous])>12288) segments++;
        segment[i]=segments-1; sizes[segments-1]++; previous=i; selected++;
    }
    for(int s=0;s<segments;s++) low[s]=2147483647;
    for(int i=0;i<n;i++) {
        int y=y0+i;
        if(chosen[i] && sizes[segment[i]]<3) {chosen[i]=0;reason[y]=7;}
        if(chosen[i]) {
            retained[y]=1;
            if(primary[y]) {
                int ridge=(rowword(p,i,2)>>27)&7;
                reason[y]=!(ridge&1)?8:!(ridge&2)?9:!(ridge&4)?10:0;
            } else reason[y]=ridge_reason(gray+(size_t)y*fw,(int)starts[y],(int)ends[y],x0,x1);
            if(!reason[y]) {
                int w=(int)(ends[y]-starts[y]+1),s=segment[i];
                if(w<low[s]) low[s]=w; if(w>high[s]) high[s]=w;
            }
        } else {centers[y]=0; starts[y]=ends[y]=-1;}
    }
    for(int i=0;i<n;i++) {
        int y=y0+i;
        if(retained[y] && !reason[y] && high[segment[i]]-low[segment[i]]>12) reason[y]=11;
        accepted[y]=retained[y] && !reason[y];
    }
    stats[0]=pl_rows; stats[1]=raw_rows; stats[2]=selected; stats[3]=segments;
    return 0;
}
