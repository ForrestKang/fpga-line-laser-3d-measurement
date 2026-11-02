/* Optional PFO3 body-field validator, ABI v1. No Raw/image algorithm.
 * All reads are bounded little-endian byte loads; no alignment/aliasing cast.
 * Caller retains the strict Python framing/identity/fingerprint/CRC checks.
 */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif
static uint32_t u32(const unsigned char *p) {
    return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24;
}
API uint32_t r25_pfo3_validator_abi(void) { return UINT32_C(0x00010019); }

/* Return codes follow the frozen Python check order, including when several
 * fields are corrupt. 100 denotes a violated body-call precondition. */
API int r25_validate_pfo3_body(const unsigned char *data, size_t length) {
    uint32_t h[64],w,t,n,i,j;
    uint32_t raw[480]={0},measure[480]={0},preview[480]={0};
    uint64_t sat250=0,sat255=0,qsum=0,msum=0,psum=0,ma_sum=0,pa_sum=0;
    unsigned char errors[24]={0};
    const unsigned char *rows;
    if (!data || length<256) return 100;
    for(i=0;i<64;i++) h[i]=u32(data+4*i);
    w=h[25];t=h[26];n=h[6];
    if(w<1 || w>752 || t<1 || t>480 || h[8]!=t ||
       n>((w+1)/2)*t || length!=(size_t)256+(size_t)64*(n+t)) return 100;
    rows=data+256+(size_t)64*n;
    for(i=0;i<t;i++) {
        uint32_t r[16];
        for(j=0;j<16;j++) r[j]=u32(rows+(size_t)64*i+4*j);
        if(r[0]!=i || r[14] || r[15]) errors[1]=1;
        if(r[1]>255 || r[3]>r[2] || r[2]>w) errors[2]=1;
        sat250+=r[2];sat255+=r[3];qsum+=r[5];msum+=r[6];psum+=r[7];
        if(r[6]>r[5] || r[7]>r[5] || r[8]>w || r[9]>w ||
           r[10]>255 || r[11]>255 || r[12]>1 || r[13]>1) errors[6]=1;
    }
    if(sat250!=h[45] || sat255!=h[44]) errors[2]=1;
    if(h[45]>h[46] || h[44]>h[45]) errors[3]=1;
    if(h[50]>5 || h[51]>5) errors[4]=1;
    if(h[58]>t || h[59]>510) errors[5]=1;
    if(h[56]>t || h[52]>510 || h[53]>2*w ||
       h[57]>t || h[54]>510 || h[55]>2*w) errors[7]=1;
    for(i=0;i<n;i++) {
        uint32_t a[16],y,s,e,dec,bits,m,p,ma,pa,cv;
        uint64_t moment,weight;
        int geometry;
        const unsigned char *at=data+256+(size_t)64*i;
        for(j=0;j<16;j++) a[j]=u32(at+4*j);
        y=a[1];s=a[2];e=a[3];dec=a[13];bits=dec>>24;
        m=(dec>>8)&255;p=(dec>>16)&255;
        ma=(bits&2)!=0;pa=(bits&4)!=0;cv=bits&1;
        if(a[0]!=i) errors[8]=1;
        geometry=y<t && s<=e && e<w;
        if(!geometry || (i && y<u32(at-64+4))) errors[9]=1;
        if(i && y==u32(at-64+4) && s<=u32(at-64+12)) errors[10]=1;
        if((bits&~UINT32_C(7)) || m>7 || p>7 || ma!=(m==0) || pa!=(p==0)) errors[11]=1;
        if(((ma||pa) && !cv) || a[4]>=w*4096) errors[12]=1;
        moment=(uint64_t)a[6] | (uint64_t)a[7]<<32;weight=a[5];
        if(geometry) {
            /* width<=752, weight is uint32, end<=751. These products cannot
             * overflow uint64. Never shift/multiply an unbounded moment. */
            int bounded=weight<=((uint64_t)e-s+1)*(255*4096) &&
                        moment<=weight*e && moment>=weight*s;
            if(!bounded || ((dec&255) && (ma||pa))) errors[13]=1;
            if(cv && weight==0) errors[14]=1;
            /* After bounded, moment <= (752*255*4096)*751 < 2^40;
             * multiplying by4096 is <2^52, even on 32-bit ARM. */
            if(cv && weight && bounded && (moment*UINT64_C(4096))/weight!=a[4]) errors[15]=1;
        }
        if((ma && (a[14]&65535)>=16*t) || (pa && (a[14]>>16)>=16*t)) errors[16]=1;
        ma_sum+=ma;pa_sum+=pa;
        if(y<t) { raw[y]++;measure[y]+=ma;preview[y]+=pa; }
    }
    if(n) {
        if(ma_sum!=h[42] || pa_sum!=h[43]) errors[17]=1;
        if((h[9]&2) && (ma_sum||pa_sum)) errors[18]=1;
    } else if(h[42] || h[43]) errors[19]=1;
    for(i=0;i<t;i++) {
        const unsigned char *r=rows+(size_t)64*i;
        if(raw[i]!=u32(r+16)) errors[20]=1;
        if(n && (measure[i]!=u32(r+24) || preview[i]!=u32(r+28))) errors[23]=1;
    }
    if(qsum!=h[7]) errors[21]=1;
    if(msum!=h[42] || psum!=h[43]) errors[22]=1;
    for(i=1;i<24;i++) if(errors[i]) return (int)i;
    return 0;
}
