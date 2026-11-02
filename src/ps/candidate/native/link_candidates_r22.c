/* Bounded PMC2 PS global linking. Pixels are deliberately absent from ABI.
 * Association order/root IDs are identical to frozen multibranch_r21.c.
 * Compile explicitly with tools/build_pl_r22_native.py; import never builds.
 */
#include <stdint.h>
#include <stdlib.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif
#define R(i,j) records[(size_t)(i)*12+(j)]
#define O(i,j) output[(size_t)(i)*3+(j)]
static int root(int *p,int i) {while(p[i]!=i){p[i]=p[p[i]];i=p[i];}return i;}
static int64_t abs64(int64_t x){return x<0?-x:x;}
API int link_candidates_r22(const uint32_t *records,int n,int height,
    int step,int residual,int minimum_rows,int32_t *output) {
    int offset[481],parent[3840],triples[3840][3],forward[3840],backward[3840],counts[3840];
    unsigned char ambiguous[3840];
    int i,j,k,y,a,b,c,nt=0,next=0,options,oi=0,ok=0,kept;
    if(n<0||n>3840||height<1||height>480||step<1||residual<1||minimum_rows<3)return -1;
    for(y=0;y<=height;++y){while(next<n&&(int)R(next,0)<y)++next;offset[y]=next;}
    for(i=0;i<n;++i){parent[i]=i;forward[i]=backward[i]=-1;counts[i]=0;ambiguous[i]=0;O(i,0)=R(i,11)&255;}
    for(y=1;y<height-1;++y)for(j=offset[y];j<offset[y+1];++j){
        if(O(j,0))continue;
        options=0;
        for(i=offset[y-1];i<offset[y];++i){
            if(O(i,0)||abs64((int64_t)R(i,5)-R(j,5))>step)continue;
            for(k=offset[y+1];k<offset[y+2];++k){
                if(O(k,0)||abs64((int64_t)R(k,5)-R(j,5))>step)continue;
                if(abs64((int64_t)R(k,5)-2*(int64_t)R(j,5)+R(i,5))<=residual){++options;oi=i;ok=k;}
            }
        }
        if(((R(j,11)>>8)&255)!=(uint32_t)options)return -2;
        if(options>1)ambiguous[j]=1;
        else if(options==1){triples[nt][0]=oi;triples[nt][1]=j;triples[nt++][2]=ok;}
    }
    kept=0;
    for(i=0;i<nt;++i){
        a=triples[i][0];b=triples[i][1];c=triples[i][2];
        if(ambiguous[a]||ambiguous[b]||ambiguous[c])continue;
        triples[kept][0]=a;triples[kept][1]=b;triples[kept++][2]=c;
        if(forward[a]==-1)forward[a]=b;else if(forward[a]!=b)forward[a]=-2;
        if(forward[b]==-1)forward[b]=c;else if(forward[b]!=c)forward[b]=-2;
        if(backward[b]==-1)backward[b]=a;else if(backward[b]!=a)backward[b]=-2;
        if(backward[c]==-1)backward[c]=b;else if(backward[c]!=b)backward[c]=-2;
    }
    nt=kept;
    for(i=0;i<n;++i)if(forward[i]==-2||backward[i]==-2)ambiguous[i]=1;
    for(i=0;i<n;++i)if(ambiguous[i])O(i,0)=11;
    for(i=0;i<nt;++i){
        a=triples[i][0];b=triples[i][1];c=triples[i][2];
        if(ambiguous[a]||ambiguous[b]||ambiguous[c])continue;
        parent[root(parent,b)]=root(parent,a);parent[root(parent,c)]=root(parent,a);
    }
    for(i=0;i<n;++i){O(i,1)=O(i,0)?-1:root(parent,i);if(O(i,1)>=0)++counts[O(i,1)];}
    for(i=0;i<n;++i){if(!O(i,0)&&counts[O(i,1)]<minimum_rows)O(i,0)=12;O(i,2)=O(i,0)==0;}
    return 0;
}
