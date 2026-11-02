"""Pure, reusable contract validation and deterministic artifact rendering."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def read_contract(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate contract key: {key}")
            result[key] = value
        return result
    result = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    errors = validate_contract(result)
    if errors:
        raise ValueError("; ".join(errors))
    return result


def validate_contract(spec: dict) -> list[str]:
    """Validate shared fields; profiles/pixel extensions are optional for reuse."""
    errors = []
    if not isinstance(spec, dict):
        return ["contract must be an object"]
    if type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        errors.append("unsupported schema_version")
    if not isinstance(spec.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", spec["name"]):
        errors.append("invalid contract name")
    constants = spec.get("constants", {})
    if not isinstance(constants, dict):
        errors.append("constants must be an object")
        constants = {}
    for name, value in constants.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or type(value) is not int or not 0 <= value < 2**64:
            errors.append(f"invalid constant {name}")
    registers = spec.get("registers")
    if not isinstance(registers, list) or not registers:
        return errors + ["registers must be a nonempty list"]
    names, offsets = set(), set()
    for reg in registers:
        if not isinstance(reg, dict):
            errors.append("register must be an object")
            continue
        name, offset = reg.get("name"), reg.get("offset")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in names:
            errors.append(f"invalid/duplicate register name: {name}")
        else:
            names.add(name)
        if type(offset) is not int or not 0 <= offset <= 0xFFFF or offset % 4 or offset in offsets:
            errors.append(f"invalid/duplicate register offset: {offset}")
        else:
            offsets.add(offset)
        if reg.get("access") not in {"ro", "rw", "command", "w1c"} or reg.get("width") != 32:
            errors.append(f"invalid access/width: {name}")
        if "reset" in reg and (type(reg["reset"]) is not int or not 0 <= reg["reset"] < 2**32):
            errors.append(f"invalid reset: {name}")
        occupied = set()
        for field in reg.get("fields", []):
            lo, hi = field.get("lsb"), field.get("msb")
            if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi < 32:
                errors.append(f"invalid field: {name}")
                continue
            bits = set(range(lo, hi+1))
            if occupied & bits:
                errors.append(f"overlapping fields: {name}")
            occupied |= bits
    for profile in spec.get("profiles", []):
        if not isinstance(profile, dict):
            errors.append("profile must be an object")
            continue
        if not isinstance(profile.get("header"), list) or profile.get("point") != ["row", "xc_fixed", "packed_flags_quality"]:
            errors.append("invalid profile layout")
        required = ["valid_bit", "flags_lsb", "flags_bits", "quality_lsb", "quality_bits", "reserved_mask"]
        if any(type(profile.get(key)) is not int for key in required):
            errors.append("missing or non-integer profile bit field")
            continue
        if not 1 <= profile["flags_bits"] <= 32 or not 1 <= profile["quality_bits"] <= 32:
            errors.append("invalid profile field width")
            continue
        ranges = [{profile["valid_bit"]}, set(range(profile["flags_lsb"], profile["flags_lsb"]+profile["flags_bits"])), set(range(profile["quality_lsb"], profile["quality_lsb"]+profile["quality_bits"]))]
        if any(any(b < 0 or b >= 32 for b in group) for group in ranges) or any(ranges[a] & ranges[b] for a,b in [(0,1),(0,2),(1,2)]):
            errors.append("overlapping/out of range point fields")
        used = set().union(*ranges)
        reserved = sum(1 << b for b in range(32) if b not in used)
        if profile.get("reserved_mask") != reserved:
            errors.append("profile reserved mask inconsistent")
    return errors


def render_artifacts(spec: dict, digest: str) -> dict[str, str]:
    constants = dict(spec.get("constants", {}))
    constants.update({"REG_" + reg["name"]: reg["offset"] for reg in spec["registers"]})
    if "regmap_version" in spec:
        constants["REGMAP_VERSION"] = spec["regmap_version"]
    if "profile_format_version" in spec:
        constants["PROFILE_FORMAT_VERSION"] = spec["profile_format_version"]
    py = [f'# Generated from interface.json SHA-256 {digest}; DO NOT EDIT.', f'CONTRACT_SHA256 = "{digest}"']
    py += [f"{name} = {value}" for name, value in sorted(constants.items())]
    py += ["REGISTERS = " + repr(spec["registers"]), "PROFILES = " + repr(spec.get("profiles", [])), ""]
    sv = [f"// Generated from interface.json SHA-256 {digest}; DO NOT EDIT.", f"package {spec['name']}_pkg;", f'  localparam string CONTRACT_SHA256 = "{digest}";']
    sv += [f"  localparam logic [63:0] {name} = 64'd{value};" for name, value in sorted(constants.items())]
    sv += ["endpackage", ""]
    doc = ["# 统一接口合同（自动生成）", "", f"源文件指纹：`{digest}`。本文件与Python/SV常量均由JSON生成，请修改源合同后重新生成。", "", "本合同不启动硬件；新RTL、Vivado及实板验证均未执行。", "", "## 寄存器", "", "寄存器版本与Profile版本分别记录。RO只读，RW配置写，command为命令脉冲，W1C写1清除。", "", "| 地址 | 名称 | 权限 | 复位值 | 位定义与限制 |", "|---|---|---|---|---|"]
    for reg in spec["registers"]:
        fields = "; ".join(f"{f['name']}[{f['msb']}:{f['lsb']}]" for f in reg.get("fields", []))
        doc.append(f"| 0x{reg['offset']:02x} | {reg['name']} | {reg['access']} | {reg.get('reset', 'runtime')} | {fields} {reg.get('notes', '')} |")
    doc += ["", "## Profile", "", "小端32bit word；包内有效位原样保留，不由flags推导。保留invalid行；缺失的frame_id、S0、S1及candidate_count为null。", "", "| 格式 | Magic | Header顺序 | valid位 | flags位起点 | 时间戳/角度语义 |", "|---|---|---|---|---|---|"]
    for p in spec.get("profiles", []):
        doc.append(f"| {p['name']} | 0x{p['magic']:08x} | {', '.join(p['header'])} | {p['valid_bit']} | {p['flags_lsb']} | {p['timestamp_semantics']}; {p['angle_semantics']} |")
    doc += ["", "Point：`row, xc_fixed, packed_flags_quality`。xc为Q10.12，quality为8bit峰值相关指标，不能称为置信概率。", "", "拒绝未知Magic、截断、额外word、重复或越界row、计数错误及非零保留位。Header的error_flags保留整个uint32；当前旧硬件仅提供camera EOF低16bit。寄存器ERROR_FLAGS另含motion/reset/capture位，两者不能混同。", "", "## 像素与ROW", "", "灰度8bit，一拍一个像素；左上为原点，x向右、y向下。ROI为半开区间，坐标保留原帧；crop不是ROI，裁剪时需要独立记录原点偏移。P0不实际裁剪图像。", "", "`w=max(gray-threshold,0)`，`S0=sum(w)`，`S1=sum(x*w)`，`xc_fixed=floor((S1<<12)/S0)`。零权重invalid。width为正权重首尾跨度，不是FWHM；多个连续正权重区间不能合成一个有效表面点。", "", "| 离线宽度 | 最坏S0 | 最坏S1 | S0/S1/分子所需bit |", "|---|---|---|---|"]
    for bounds in spec.get("centroid", {}).get("worst_case", []):
        doc.append(f"| {bounds['width']} | {bounds['max_s0']} | {bounds['max_s1']} | {bounds['s0_bits']}/{bounds['s1_bits']}/{bounds['numerator_bits']} |")
    doc += ["", "752候选位宽18/27/39bit仅做范围验证；未修改旧640宽RTL。", "", "## 时钟、单位与未知硬件", "", "system_clk目标100MHz；CAM48/60MHz为后续时序预算。SLP2时间戳锁存在采集请求被接受时，10ns tick不能解释为10ns曝光同步精度。SLP1时间戳单位不明，保留raw。", "", "距离mm，内部角度rad，显示degree。物理帧尺寸、极性、管脚和扩展板接线未确认；板级模板保留null且禁止运行。离线默认640/752不能自动回填真实硬件。", ""]
    return {"src/python/structured_light/generated.py": "\n".join(py), "src/rtl/generated/interface_pkg.sv": "\n".join(sv), "report/interfaces.md": "\n".join(doc)}


def expected_artifacts(root: Path) -> dict[str, str]:
    source = root / "src/contracts/interface.json"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return render_artifacts(read_contract(source), digest)
