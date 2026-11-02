"""Offline exact packet comparison with a narrow processing-cycle exception."""
import argparse
import hashlib
import json
import struct
from pathlib import Path


def compare(baseline, candidate):
    old = json.loads((baseline / "RESULT.json").read_text())
    new = json.loads((candidate / "RESULT.json").read_text())
    if not old.get("passed") or not new.get("passed"):
        raise ValueError("both regressions must pass")
    names = [item["case"] for item in old["cases"]]
    if names != [item["case"] for item in new["cases"]]:
        raise ValueError("case lists differ")
    cases = []
    for name in names:
        a = (baseline / (name + "_primary.bin")).read_bytes()
        b = (candidate / (name + "_primary.bin")).read_bytes()
        if len(a) != len(b) or len(a) % 4 or len(a) < 384:
            raise ValueError(name + ": packet size mismatch")
        wa = struct.unpack("<%dI" % (len(a) // 4), a)
        wb = struct.unpack("<%dI" % (len(b) // 4), b)
        allowed = {len(wa) - 32 + 14, len(wa) - 32 + 15}
        changed = [i for i, pair in enumerate(zip(wa, wb)) if pair[0] != pair[1]]
        if set(changed) - allowed:
            raise ValueError(name + ": non-cycle packet field changed")
        before = wa[-18] | (wa[-17] << 32)
        after = wb[-18] | (wb[-17] << 32)
        cases.append(dict(case=name, bytes=len(a), changed_word_indices=changed,
                          before_cycles=before, after_cycles=after,
                          delta_cycles=after-before,
                          baseline_sha256=hashlib.sha256(a).hexdigest(),
                          candidate_sha256=hashlib.sha256(b).hexdigest(),
                          all_non_cycle_bytes_equal=True))
    return dict(passed=True, cases=cases,
                exception="footer processing_cycles low/high words only; no timestamp exception",
                scope="offline RTL packets, not board performance")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    value = compare(args.baseline, args.candidate)
    with args.output.open("x", encoding="utf8") as out:
        json.dump(value, out, indent=2)
    print(json.dumps(dict(passed=True, cases=len(value["cases"]))))


if __name__ == "__main__":
    main()
