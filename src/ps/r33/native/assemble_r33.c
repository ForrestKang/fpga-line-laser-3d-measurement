/* R33 body validator/patch assembler. No pixel processing or branch inference. */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#if defined(_WIN32)
#define API __declspec(dllexport)
#else
#define API __attribute__((visibility("default")))
#endif
#define MAX_N 180480u
#define H 480u
#define W 752u
static uint32_t rd(const unsigned char *p) { uint32_t v; memcpy(&v,p,4); return v; }
#define E(k) rd(end+4u*(k))
#define A(i,k) rd(data+256u+68u*(i)+4u*(k))
/* Error codes preserve reference validation precedence and messages. */
API int r33_body(const unsigned char *data, size_t length, uint32_t *out,
                 size_t capacity, uint32_t *stats) {
    size_t count, i; uint32_t n=0,nr=0,np=0;
    const unsigned char *end;
    uint32_t *rows; unsigned char *expected=NULL,*seen=NULL;
    uint32_t lc[H]={0},qc[H]={0},mc[H]={0},pc[H]={0};
    uint64_t sm=0,sp=0,sn=0,sq=0,s250=0,s255=0;
    uint32_t q=0,expect_count=0,mrows=0,prows=0; int overflow=0,err=0;
    if(length<384u || (length-384u)%68u) return 90;
    count=(length-384u)/68u;end=data+length-128u;
    for(i=0;i<count;i++) {
        uint32_t tag=A(i,0);
        if((tag&0xfffffffcu)!=0x33520000u || (tag&3u)==3u) return 1;
        if((tag&3u)==0u)n++; else if((tag&3u)==1u)np++; else nr++;
    }
    if(n>MAX_N || n!=E(2) || nr!=H || E(3)>n) return 2;
    if(capacity<(size_t)(n+H)*64u) return 91;
    rows=out+(size_t)n*16u;
    {uint32_t li=0,ri=0;
     for(i=0;i<count;i++) {
        uint32_t kind=A(i,0)&3u;
        if(kind==0u)memcpy(out+(size_t)li++*16u,data+260u+68u*i,64u);
        else if(kind==2u)memcpy(rows+(size_t)ri++*16u,data+260u+68u*i,64u);
     }}
    for(i=0;i<n;i++)if(out[i*16u]!=i)return 3;
    for(i=0;i<H;i++)if(rows[i*16u]!=i)return 3;
    for(i=0;i<H;i++)if(rows[i*16u+14u] || rows[i*16u+15u])return 4;
    for(i=0;i<n;i++) {
        uint32_t *r=out+i*16u;
        if(r[1]>=H || r[2]>r[3] || r[3]>=W || (i && r[1]<out[(i-1u)*16u+1u]))return 5;
    }
    for(i=0;i<n;i++) {
        uint32_t *r=out+i*16u; int64_t dv=(int64_t)r[5]-((int64_t)r[1]<<12);
        if(((r[13]>>24)&1u) && (r[4]>=(W<<12) || r[5]>=(H<<12) || dv>2048 || dv< -2048))return 6;
    }
    expected=(unsigned char*)calloc(n?n:1u,1u);seen=(unsigned char*)calloc(n?n:1u,1u);
    if(!expected || !seen){err=92;goto done;}
    for(i=0;i<n;i++) {
        uint32_t *r=out+i*16u; lc[r[1]]++;
        if((r[13]&255u)==0u) {q++;if(qc[r[1]]++<16u){expected[i]=1;expect_count++;}}
    }
    if(q!=E(3)){err=7;goto done;}
    if(np!=expect_count){err=8;goto done;}
    for(i=0;i<count;i++)if((A(i,0)&3u)==1u) {
        uint32_t id=A(i,1);
        if(id>=n || seen[id] || !expected[id]){err=8;goto done;}
        seen[id]=1;
    }
    for(i=0;i<count;i++)if((A(i,0)&3u)==1u && (A(i,14)&255u)){err=9;goto done;}
    for(i=0;i<count;i++)if((A(i,0)&3u)==1u) {
        uint32_t id=A(i,1),*r=out+(size_t)id*16u;
        r[13]=(r[13]&255u)|(A(i,14)&0xffffff00u);
        r[14]=A(i,15);r[15]=(r[15]&0xff000000u)|(A(i,16)&0xffffffu);
    }
    /* Final patches may enable center_valid; coordinates must be checked again. */
    for(i=0;i<n;i++) {
        uint32_t *r=out+i*16u; int64_t dv=(int64_t)r[5]-((int64_t)r[1]<<12);
        if(((r[13]>>24)&1u) && (r[4]>=(W<<12) || r[5]>=(H<<12) || dv>2048 || dv< -2048)){
            err=6;goto done;
        }
    }
    if((E(11)&~63u) || !(E(11)&1u)){err=10;goto done;}
    for(i=0;i<n;i++) {
        uint32_t d=out[i*16u+13u],m=(d>>25)&1u,p=(d>>26)&1u;
        if((m&&!p) || ((m||p)&&((d&255u)!=0u || !((d>>24)&1u)))){err=11;goto done;}
    }
    for(i=0;i<n;i++)if(out[i*16u+13u]&(0xf8000000u|192u)){err=12;goto done;}
    for(i=0;i<n;i++) {
        uint32_t *r=out+i*16u,d=r[13],m=(d>>25)&1u,p=(d>>26)&1u;
        if((m&&((d>>8)&255u)) || (p&&((d>>16)&255u))){err=13;goto done;}
        if(m){sm++;mc[r[1]]++;} if(p){sp++;pc[r[1]]++;}
    }
    for(i=0;i<H;i++){mrows+=(mc[i]!=0u);prows+=(pc[i]!=0u);}
    if(sm!=E(4)||sp!=E(5)||mrows!=E(6)||prows!=E(7)){err=14;goto done;}
    for(i=0;i<H;i++){uint32_t *r=rows+i*16u;sn+=r[4];sq+=r[5];s250+=r[2];s255+=r[3];}
    if(sn!=n||sq!=q||s250!=E(9)||s255!=E(10)){err=15;goto done;}
    for(i=0;i<H;i++)if(lc[i]!=rows[i*16u+4u]){err=16;goto done;}
    for(i=0;i<H;i++)if(qc[i]!=rows[i*16u+5u]){err=17;goto done;}
    for(i=0;i<H;i++)if(mc[i]!=rows[i*16u+6u]||pc[i]!=rows[i*16u+7u]){err=18;goto done;}
    /* R39: an overflowed row (>16 qualified) may accept nothing; other rows may. */
    for(i=0;i<H;i++)if(rows[i*16u+5u]>16u){overflow=1;if(mc[i]||pc[i]){err=19;goto done;}}
    if(E(11)!=(overflow?11u:1u)){err=19;goto done;}
    {int usable=E(4)>=8u && (uint64_t)E(6)*4u>=H &&
           (uint64_t)E(9)*1000u<=(uint64_t)W*H && (uint64_t)E(8)*10u<=np;
     if(E(25)!=(uint32_t)usable || E(26)!=(uint32_t)(sp!=0u) || (E(27)&~127u)){err=20;goto done;}
     if(!!(E(27)&1u)!=(sp==0u)||!!(E(27)&2u)!=!usable||!!(E(27)&4u)!=overflow){err=21;goto done;}}
    stats[0]=n;stats[1]=q;stats[2]=np;
 done:free(expected);free(seen);return err;
}
API uint32_t r33_body_abi(void){return 0x330001u;}
