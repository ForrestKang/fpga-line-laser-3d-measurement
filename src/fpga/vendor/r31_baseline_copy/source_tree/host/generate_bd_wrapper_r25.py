"""Vivado module references require a Verilog top wrapper; math stays in SV."""
from pathlib import Path
import re
root=Path(__file__).resolve().parents[1]
text=(root/'pl/integration/axi_packet_controller.sv').read_text()
ports=text.split('parameter FRAME_WIDTH=752,FRAME_HEIGHT=480)(',1)[1].split('\n);',1)[0]
names=re.findall(r'\b(?:input|output)\s+(?:wire|reg)\s+(?:\[[^\]]+\]\s+)?(\w+)',ports)
ports=re.sub(r'\boutput reg\b','output wire',ports)
result='// Generated transport wrapper, no image computation.\nmodule pfo3_axi_bd #(parameter FRAME_WIDTH=752,FRAME_HEIGHT=480)(\n'+ports+'\n);\n'
result+='axi_packet_controller #(.FRAME_WIDTH(FRAME_WIDTH),.FRAME_HEIGHT(FRAME_HEIGHT)) core(\n'
result+=',\n'.join('.'+x+'('+x+')' for x in names)+'\n);\nendmodule\n'
(root/'pl/integration/pfo3_axi_bd.v').write_text(result)
print('generated ports',len(names))
