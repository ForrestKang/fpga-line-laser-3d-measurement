/* Exact fixed-threshold implementation of frozen multi_stripe.py.
 * No geometry, shape model, interpolation, camera or device access.
 * Build: gcc -O3 -std=c99 -fPIC -shared multibranch_r21.c -o libmultibranch_r21.so
 */
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include <limits.h>

#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif

/* Per-candidate int32 columns: row,start,end,width,edge,peaks,reason,branch,accepted. */
#define COLS 9
#define AT(i,j) out[(size_t)(i)*COLS+(j)]
enum { WIDTH=1, CONTRAST, SATURATED, TRUNCATED, OVERFLOW, ROW_OVERFLOW,
       BACKGROUND, RIDGE_CONTRAST, RIDGE_SHAPE, MERGED, AMBIGUOUS, SUPPORT };

API int64_t multibranch_count(const uint8_t *im, int w, int h, const int *cfg) {
    int x,y,inside; int64_t n=0;
    (void)h;
    for(y=cfg[1];y<cfg[3];++y) {
        inside=0;
        for(x=cfg[0];x<cfg[2];++x) {
            int bright=im[(size_t)y*w+x]>cfg[4];
            if(bright&&!inside) ++n;
            inside=bright;
        }
    }
    return n;
}

static double median_small(const uint8_t *a, int n) {
    int v[3],i,j,t;
    for(i=0;i<n;++i) v[i]=a[i];
    for(i=1;i<n;++i) for(j=i;j>0&&v[j]<v[j-1];--j) {t=v[j];v[j]=v[j-1];v[j-1]=t;}
    return (v[(n-1)/2]+v[n/2])*.5;
}

static int find_root(int *parent, int i) {
    while(parent[i]!=i) { parent[i]=parent[parent[i]]; i=parent[i]; }
    return i;
}

API int multibranch_extract(const uint8_t *im, int w, int h, const int *cfg,
                           const int *pol, const double *limits, int n,
                           int32_t *out, int64_t *q, double *contrast) {
    int x,y,i,j,k,a,b,c,start,end,idx=0,reason,peak,saturated,pc,last_peak;
    int *offset=NULL,*parent=NULL,*triples=NULL,*forward=NULL,*backward=NULL,*counts=NULL;
    unsigned char *ambig=NULL;
    const int x0=cfg[0],y0=cfg[1],x1=cfg[2],y1=cfg[3],threshold=cfg[4];
    const double step=limits[0]*4096., residual=limits[1]*4096.;
    int nt=0;
    offset=(int*)calloc((size_t)h+1,sizeof(int));
    parent=(int*)malloc(((size_t)n+1)*sizeof(int));
    triples=(int*)malloc(((size_t)n+1)*3*sizeof(int));
    forward=(int*)malloc(((size_t)n+1)*sizeof(int));
    backward=(int*)malloc(((size_t)n+1)*sizeof(int));
    counts=(int*)calloc((size_t)n+1,sizeof(int));
    ambig=(unsigned char*)calloc((size_t)n+1,1);
    if(!offset||!parent||!triples||!forward||!backward||!counts||!ambig) {reason=-1;goto cleanup;}
    for(y=0;y<h;++y) {
        const uint8_t *row=im+(size_t)y*w;
        offset[y]=idx;
        if(y<y0||y>=y1) continue;
        x=x0;
        while(x<x1) {
            int64_t s0=0,s1=0;
            while(x<x1&&row[x]<=threshold) ++x;
            if(x==x1) break;
            start=x;peak=0;saturated=0;
            while(x<x1&&row[x]>threshold) {
                int weight=row[x]-threshold;
                s0+=weight;s1+=(int64_t)weight*x;
                if(weight>peak) peak=weight;
                if(row[x]>=cfg[10]) ++saturated;
                ++x;
            }
            end=x-1;
            if(idx>=n) {reason=-2;goto cleanup;}
            AT(idx,0)=y;AT(idx,1)=start;AT(idx,2)=end;AT(idx,3)=end-start+1;
            q[idx]=(s1*4096)/s0;
            reason=0;
            if(end-start+1<cfg[5]||end-start+1>cfg[6]) reason=WIDTH;
            if(!reason&&(s0<cfg[7]||peak<cfg[8])) reason=CONTRAST;
            if(!reason&&saturated) reason=SATURATED;
            if(!reason&&(start==x0||end==x1-1)) reason=TRUNCATED;
            if(!reason&&(s0>262143||s1>134217727||q[idx]>4194303)) reason=OVERFLOW;
            /* Plateau peaks, excluding either endpoint; merged overrides all reasons. */
            pc=0;last_peak=-1;
            for(a=start;a<=end;a=b) {
                b=a+1;while(b<=end&&row[b]==row[a]) ++b;
                if(a>start&&b<=end&&row[a]>row[a-1]&&row[b-1]>row[b]) {
                    if(last_peak>=0) {
                        int saddle=255,smaller=row[last_peak]<row[a]?row[last_peak]:row[a];
                        for(c=last_peak;c<=a;++c) if(row[c]<saddle) saddle=row[c];
                        if(smaller-saddle>=pol[2]) reason=MERGED;
                    }
                    last_peak=b-1;++pc;
                }
            }
            AT(idx,5)=pc;contrast[idx]=0.;AT(idx,4)=0;
            if(start<=x0||end>=x1-1) {if(!reason) reason=BACKGROUND;}
            else {
                double left,right,background;
                int ln=start-x0<3?start-x0:3,rn=x1-end-1<3?x1-end-1:3;
                int maximum=peak+threshold,ld=maximum-row[start],rd=maximum-row[end];
                left=median_small(row+start-ln,ln);right=median_small(row+end+1,rn);
                background=left>right?left:right;
                contrast[idx]=maximum-background;AT(idx,4)=ld<rd?ld:rd;
                if(!reason&&contrast[idx]<pol[0]) reason=RIDGE_CONTRAST;
                if(!reason&&(end-start+1<3||AT(idx,4)<pol[1]||pc==0)) reason=RIDGE_SHAPE;
            }
            AT(idx,6)=reason;parent[idx]=idx;forward[idx]=-1;backward[idx]=-1;++idx;
        }
        if(idx-offset[y]>cfg[9]) {
            for(i=offset[y];i<idx;++i) {
                /* Frozen reference applies overflow before resolved-peak override. */
                if(AT(i,6)!=MERGED) AT(i,6)=ROW_OVERFLOW;
            }
        }
    }
    offset[h]=idx;
    if(idx!=n) {reason=-2;goto cleanup;}
    for(y=1;y<h-1;++y) for(j=offset[y];j<offset[y+1];++j) {
        int options=0,oi=-1,ok=-1;
        if(AT(j,6)) continue;
        for(i=offset[y-1];i<offset[y];++i) {
            if(AT(i,6)||fabs((double)(q[i]-q[j]))>step) continue;
            for(k=offset[y+1];k<offset[y+2];++k) {
                if(AT(k,6)||fabs((double)(q[k]-q[j]))>step) continue;
                if(fabs((double)(q[k]-2*q[j]+q[i]))<=residual) {++options;oi=i;ok=k;}
            }
        }
        if(options>1) ambig[j]=1;
        else if(options==1) {triples[3*nt]=oi;triples[3*nt+1]=j;triples[3*nt+2]=ok;++nt;}
    }
    /* Filter triples against the first ambiguity pass before building links. */
    k=0;
    for(i=0;i<nt;++i) {
        a=triples[3*i];b=triples[3*i+1];c=triples[3*i+2];
        if(ambig[a]||ambig[b]||ambig[c]) continue;
        triples[3*k]=a;triples[3*k+1]=b;triples[3*k+2]=c;++k;
        if(forward[a]==-1) forward[a]=b; else if(forward[a]!=b) forward[a]=-2;
        if(forward[b]==-1) forward[b]=c; else if(forward[b]!=c) forward[b]=-2;
        if(backward[b]==-1) backward[b]=a; else if(backward[b]!=a) backward[b]=-2;
        if(backward[c]==-1) backward[c]=b; else if(backward[c]!=b) backward[c]=-2;
    }
    nt=k;
    for(i=0;i<n;++i) if(forward[i]==-2||backward[i]==-2) ambig[i]=1;
    for(i=0;i<n;++i) if(ambig[i]) AT(i,6)=AMBIGUOUS;
    for(i=0;i<nt;++i) {
        a=triples[3*i];b=triples[3*i+1];c=triples[3*i+2];
        if(ambig[a]||ambig[b]||ambig[c]) continue;
        parent[find_root(parent,b)]=find_root(parent,a);
        parent[find_root(parent,c)]=find_root(parent,a);
    }
    for(i=0;i<n;++i) {
        AT(i,7)=AT(i,6)?-1:find_root(parent,i);
        if(AT(i,7)>=0) ++counts[AT(i,7)];
    }
    for(i=0;i<n;++i) {
        if(!AT(i,6)&&counts[AT(i,7)]<pol[3]) AT(i,6)=SUPPORT;
        AT(i,8)=AT(i,6)==0;
    }
    reason=0;
cleanup:
    free(offset);free(parent);free(triples);free(forward);free(backward);free(counts);free(ambig);
    return reason;
}
