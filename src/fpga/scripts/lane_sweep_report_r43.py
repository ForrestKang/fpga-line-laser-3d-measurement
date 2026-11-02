"""Summarize receipt-backed R43 lane experiments without board claims."""
from pathlib import Path
import hashlib,json,re
R=Path(__file__).resolve().parents[1];E=R/'evidence'
experiments=[]
for p in sorted(E.glob('LANE_SWEEP_R43_*/RESULT.json')):
 x=json.loads(p.read_text());experiments.append({'path':str(p.relative_to(R)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'passed':x['passed'],'dispatch':x.get('dispatch',0),'banked_requests':x.get('banked_requests',0),'banked_results':x.get('banked_results',0),'balanced_dispatch':x.get('balanced_dispatch',0),'cases':x['cases'],'lanes':x['lanes'],'results':x['results'],'sources':x['sources']})
# Independent cross-experiment comparison covers synthetic cases even when a
# particular run's optional baseline directory only contains T05/T06/noise.
first={};comparisons=0
for x in experiments:
 d=R/Path(x['path']).parent
 for z in x['results']:
  for suffix in ('records','geometry'):
   p=d/f"{z['case']}_l{z['lanes']}.{suffix}"
   key=(z['case'],suffix)
   if key in first:assert p.read_bytes()==first[key].read_bytes(),str(p)
   else:first[key]=p
   comparisons+=1
cross_experiment=dict(passed=True,files_compared=comparisons,cases=sorted(set(k[0] for k in first)))
ooc=[]
for p in sorted(E.glob('LANE_OOC_R43_*/*/RESULT.txt')):
 d=p.parent;u=(d/'utilization.rpt').read_text();t=(d/'timing_synth.rpt').read_text();top=next(v for v in u.splitlines() if '| r35_row ' in v);f=[v.strip() for v in top.split('|')][1:-1];ls=t.splitlines();i=next(i for i,v in enumerate(ls) if 'WNS(ns)' in v);v=ls[i+2].split()
 ooc.append(dict(path=str(d.relative_to(R)),total_lut=int(f[2]),ff=int(f[6]),ramb18=int(f[8]),dsp=int(f[9]),synthesis_wns_ns=float(v[0]),synthesis_whs_ns=float(v[4]),routed=False,sources={str(q.relative_to(d)):hashlib.sha256(q.read_bytes()).hexdigest() for q in (d/'sources').glob('*')}))
controls=[]
for p in sorted(E.glob('LANE_CONTROL_R43_*/RESULT.json')):
 x=json.loads(p.read_text());controls.append({'path':str(p.relative_to(R)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),**x})
result=dict(legacy_geometry_field_meaning='2D observations sent to geometry kernel; not reconstructed XYZ',cross_experiment_byte_equal=cross_experiment,evidence_kind='RTL_FULL_FRAME_AND_ROW_OOC_NOT_BOARD',resolution=[752,480],experiments=experiments,ooc=ooc,controls=controls,source_sha256={str(p.relative_to(R)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [R/'rtl/r35_row.sv',R/'rtl/r35_frontend.sv',R/'tests/lane_sweep_tb.sv']},unverified=['full 480-frame sequence','routed implementation of lane variants','board replay','end-to-end 10 Hz','optical accuracy','multiscale/normal algorithm enhancements'])
(E/'LANE_FINAL_R43.json').write_text(json.dumps(result,indent=2))
lines=['# R43方向通道、调度和银行化实测收口','', '证据范围：本报告为实际执行的752×480完整前端RTL仿真与行引擎OOC综合。没有上板、没有整板布局布线、没有修改算法。默认仍为2路静态/共享存储；增强由显式编译参数选择。','', '## 已实现','', '- frontend支持ROW_LANES/ROW_DISPATCH/ROW_BANKED_REQUESTS/ROW_BANKED_RESULTS/ROW_BALANCED_DISPATCH；宏分别为R43_ROW_LANES、R43_ROW_DISPATCH、R43_BANKED_REQUESTS、R43_BANKED_RESULTS、R43_BALANCED_DISPATCH，默认2/0/0/0/0。','- 动态调度选择排队+执行数最少的通道，轮转打破平局；全局ID按序退休，全部候选诊断和送往几何核的二维观测输出不变。','- 可选平衡调度器使用小位宽load与轮转距离组合key、平衡比较树；8路综合WNS−4.141→−1.431ns，仍负，因此保留失败、不再修改。最终同一RTL的4路平衡动态配置综合WNS为+1.560ns；8路静态银行配置绕开动态反馈，综合WNS为+1.917ns。默认关闭。','- 可选每路4深60位feature FIFO，独立generate推断分布RAM；可选每路单写51位结果RAM、全局owner按序选择，消除多写口结果交叉网络。有效性由计数、完成位与帧clear控制，payload RAM不作无效清零。','', '## 周期矩阵（100MHz时钟；周期不等于板端帧率）','', '|输入|2静态|4静态|8静态|2动态|4动态|8动态|','|---|---:|---:|---:|---:|---:|---:|']
lookup={}
for x in experiments:
 if x['banked_requests'] or x['banked_results']:continue
 for z in x['results']:lookup[(z['case'],z['lanes'],x['dispatch'])]=z
for case in ['T05','T06','noise','cluster16','dense16','wide','wide_sharp','slots17','empty']:
 vals=[str(lookup[(case,n,d)]['cycles']) if (case,n,d) in lookup else '未测' for d,n in [(0,2),(0,4),(0,8),(1,2),(1,4),(1,8)]]
 lines.append('|'+case+'|'+'|'.join(vals)+'|')
lines+=['','## 最终冻结配置全尺寸矩阵','','|输入|2动态参考周期|4平衡动态+银行周期|8静态+银行周期|送往几何核的二维观测条数|','|---|---:|---:|---:|---:|']
final={}
for x in experiments:
 if '08_STATIC_BANKED' in x['path'] or '09_BALANCED4' in x['path']:
  for z in x['results']:final[(z['case'],z['lanes'])]=z
for case in ['T05','T06','noise','cluster16','dense16','wide','wide_sharp','slots17','empty']:
 a=lookup.get((case,2,1));b=final.get((case,4));c=final.get((case,8));obs=(b or c or a or {}).get('geometry','未测')
 lines.append('|'+case+'|'+str(a['cycles'] if a else '未测')+'|'+str(b['cycles'] if b else '未测')+'|'+str(c['cycles'] if c else '未测')+'|'+str(obs)+'|')
lines+=['','T05/T06每种通道配置周期相同，CLOSE阻塞均为0，因此该两帧方向引擎不是前端瓶颈。T05候选共331个，静态2路分322/9，动态分166/165，但平衡负载没有改变总周期。','', '2动态相对2静态：noise周期下降45.5%，cluster下降约6.9%，slots17下降约8.2%。noise没有有效二维观测，属于拒绝/溢出压力测试，不能据此宣传正常模型提速。dense16/cluster后处理时间占主要部分，增加通道并未改善。宽高斯wide图没有检测候选，属于当前检测规则的失败输入；宽台阶边缘wide_sharp有3824条二维观测。','', '## OOC综合资源与时序（非route）','', '|配置路径|总LUT|FF|RAMB18|DSP|综合WNS ns|综合WHS ns|','|---|---:|---:|---:|---:|---:|---:|']
for x in ooc:lines.append('|'+x['path']+'|'+'|'.join(str(x[k]) for k in ['total_lut','ff','ramb18','dsp','synthesis_wns_ns','synthesis_whs_ns'])+'|')
lines+=['','结果银行8路将feature银行版20344 LUT降至8582（−57.8%），证明全局多写results存储是重要膨胀源。旧动态8路综合WNS−4.141ns，最差路径direction ready反馈到metadata写使能，15级LUT、估计布线占80.97%；平衡树8路仍为−1.431ns，保留失败。最终4路银行+平衡动态配置为4617 LUT/56 DSP、WNS+1.560ns；8路银行静态为8531 LUT/112 DSP、WNS+1.917ns。综合正裕量仅构成完整实现的候选准入，不能代替route、CDC或板端收益。T05/T06正常负载未见通道数收益，不以更多DSP占用作为发布依据。','', '## 等价与控制验证','']
for x in experiments:lines.append('- '+x['path']+': '+str(len(x['results']))+'个全尺寸输入；记录、全部摘要字段（周期除外）和送往几何核的二维观测输出逐字节对照；确定性输出背压通过。')
for x in controls:lines.append('- '+x['path']+': '+str(len(x['tests']))+'种配置通过活动帧abort、元数据/有效状态clear、随后完整T05与黄金逐字节一致、背压输出稳定。')
lines+=['','测试对象是冻结T05/T06各第1帧与合成压力图，并非480帧全序列。没有将这些局部验证当作整板或光学证据。实际保存的来源快照/输出SHA见LANE_FINAL_R43.json及各实验RESULT。早期OOC01的2/4路没有就地源码快照，属于历史探索值，不能充当可交付构建来源；后续02–07均有编译前源码副本。','', '## 共享窗口/双尺度/法向下一阶段设计审计','', '- 现核共享3×3窗口、2个RAMB18；方向核每路14DSP，以候选请求驱动。T05总方向busy8625周期，相对2路448813周期容量利用约0.96%，继续复制DSP不会改善该输入。','- 完整7×7原始窗口需6×752×8=36096位，但多行端口组织通常需6个RAMB18。可分离5/7尺度的中间值按18位预算、4+6行共135360位，通常10个RAMB18；连同原始窗口为16个RAMB18的架构预算，尚非综合结果。必须替换/复用现有存储而非叠加。','- 两尺度对称5/7tap每轴分别3/4个真实乘法，横纵共14个乘法通路；若系数固定为二项式，综合移位加法可能更合理，不能强制DSP凑数。Hessian的Gxx/Gxy/Gyy和法向插值另行列出位宽、周期、复用表再确定DSP数。','- 多尺度/法向增强改变中心语义，应独立算法版本、二维Q12合同、高精度与定点参考、独立真值和留出集；本轮未实现，未混入等价性能版本。','', '## 尚未验证','', '- 平衡树8路只完成综合；因时序负不进入完整仿真/整板，无验证通过声明。','- 480帧多帧全序列、实际整板route/CDC、板端回灌和完整链10Hz由主代理后续统一实施。','- 输入断流/反复复位的完整矩阵本轮未独立展开；活动abort和跨帧污染已实际覆盖。这里是frontend的abort端口控制验证，不能替代AXI STOP寄存器解码及系统级停止总链测试。','- 未操作板卡，12V未涉及；R42及旧包未修改。']
(R/'REPORT_LANES_R43.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print('WROTE',len(experiments),len(ooc),len(controls))
