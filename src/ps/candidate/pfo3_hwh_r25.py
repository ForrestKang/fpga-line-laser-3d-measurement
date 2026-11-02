"""Exact PFO3 hardware preflight, independent of legacy StageB checks."""
import xml.etree.ElementTree as ET

def audit_hwh(path):
    root=ET.parse(path).getroot()
    def module(name):
        rows=[m for m in root.findall('.//MODULE') if m.get('INSTANCE')==name]
        if len(rows)!=1:raise ValueError('exactly one HWH module required: '+name)
        return rows[0]
    dma=module('pfo3_dma');frontend=module('pfo3_frontend');legacy=module('processing_pipeline')
    params={}
    for p in dma.findall('./PARAMETERS/PARAMETER'):
        params.setdefault(p.get('NAME').lower(),[]).append(p.get('VALUE'))
    values=dict(c_include_sg=1,c_include_mm2s=1,c_include_s2mm=1,c_sg_length_width=24,
        c_sg_include_stscntrl_strm=0,c_include_mm2s_dre=0,c_include_s2mm_dre=0,
        c_m_axis_mm2s_tdata_width=32,c_s_axis_s2mm_tdata_width=32,
        c_m_axi_mm2s_data_width=64,c_m_axi_s2mm_data_width=64)
    for key,value in values.items():
        if not params.get(key) or {int(raw,0) for raw in params[key]}!={value}:
            raise ValueError('HWH DMA parameter mismatch: '+key)
    if frontend.get('MODTYPE')!='pfo3_axi_bd_r25':raise ValueError('wrong PFO3 module reference')
    frame_params={p.get('NAME'):p.get('VALUE') for p in frontend.findall('./PARAMETERS/PARAMETER')}
    if frame_params.get('FRAME_WIDTH')!='752' or frame_params.get('FRAME_HEIGHT')!='480':
        raise ValueError('PFO3 native frame dimensions mismatch')
    for instance,base,high in (('pfo3_frontend',0x40000000,0x40000fff),('pfo3_dma',0x40430000,0x4043ffff)):
        ranges={(int(r.get('BASEVALUE'),0),int(r.get('HIGHVALUE'),0)) for r in root.findall('.//MEMRANGE')
                if r.get('INSTANCE')==instance and r.get('MEMTYPE')=='REGISTER'}
        if ranges!={(base,high)}:raise ValueError('HWH register map mismatch: '+instance)
    for mod,clocks,reset in ((frontend,('aclk',),'aresetn'),
            (dma,('s_axi_lite_aclk','m_axi_mm2s_aclk','m_axi_s2mm_aclk','m_axi_sg_aclk'),'axi_resetn')):
        ports={p.get('NAME'):p for p in mod.findall('./PORTS/PORT')}
        for clock in clocks:
            p=ports.get(clock)
            if p is None or p.get('CLKFREQUENCY')!='100000000' or p.get('SIGNAME')!='ps7_FCLK_CLK0':
                raise ValueError('HWH clock contract mismatch: '+clock)
        if ports[reset].get('SIGNAME')!='system_reset_peripheral_aresetn':raise ValueError('HWH reset mismatch')
    legacy_params={p.get('NAME'):p.get('VALUE') for p in legacy.findall('./PARAMETERS/PARAMETER')}
    for key in ('COMPACT_POLICY_ENABLE','FEATURE_DMA_ENABLE','MULTI_CANDIDATE_ENABLE','RHI_ENABLE'):
        if legacy_params.get(key)!='0':raise ValueError('historical algorithm not pruned: '+key)
    fbus={b.get('NAME'):b.get('BUSNAME') for b in frontend.findall('./BUSINTERFACES/BUSINTERFACE')}
    dbus={b.get('NAME'):b.get('BUSNAME') for b in dma.findall('./BUSINTERFACES/BUSINTERFACE')}
    if (fbus.get('S_AXIS')!=dbus.get('M_AXIS_MM2S') or fbus.get('M_AXIS')!=dbus.get('S_AXIS_S2MM') or
            fbus.get('S_AXIS')!='pfo3_dma_M_AXIS_MM2S' or fbus.get('M_AXIS')!='pfo3_frontend_M_AXIS'):
        raise ValueError('HWH shared PL stream wiring mismatch')
    for master in ('M_AXI_SG','M_AXI_MM2S','M_AXI_S2MM'):
        r=[r for r in dma.findall('.//MEMRANGE') if r.get('MASTERBUSINTERFACE')==master]
        if (len(r)!=1 or r[0].get('SLAVEBUSINTERFACE')!='S_AXI_HP2' or
                int(r[0].get('BASEVALUE'),0)!=0 or int(r[0].get('HIGHVALUE'),0)!=0x1fffffff):
            raise ValueError('HWH DDR HP2 path mismatch: '+master)
    return dict(passed=True,clock_hz=100000000,SG_application_streams_disabled=True,
                pfo3_base=0x40000000,dma_base=0x40430000,legacy_algorithm_modes_disabled=True)
