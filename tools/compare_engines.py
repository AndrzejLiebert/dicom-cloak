"""Compare the outputs of the two engines file by file (same relative paths, headers + private blobs).

  python tools/compare_engines.py PY_OUT CTP_OUT

Both runs must use the same input, config and pseudonyms. UIDs are compared structurally only (both are
2.25.*, from different keyed hashes); DeidentificationMethod differs by design. Prints counts of differing
attribute paths (names only, no values). Expected result: no differences, except SpecificCharacterSet
'ISO_IR 100' that CTP inserts where the source had none.
"""
import collections
import hashlib
import os
import sys

import pydicom


def flat(ds, pre=""):
    out = {}
    for e in ds:
        if e.tag == 0x7FE00010 or e.keyword == "DeidentificationMethod":
            continue
        k = pre + (e.keyword or str(e.tag))
        if e.VR == "SQ":
            out[k] = f"SQ[{len(e.value)}]"
            for i, it in enumerate(e.value):
                out.update(flat(it, f"{k}[{i}]."))
        elif e.VR == "UI" and str(e.value).startswith("2.25."):
            out[k] = "2.25.*"
        elif isinstance(e.value, (bytes, bytearray)):
            out[k] = "bytes:" + hashlib.sha1(bytes(e.value)).hexdigest()
        else:
            out[k] = (e.VR, str(e.value))
    return out


def main():
    a, b = sys.argv[1], sys.argv[2]
    diff, n = collections.Counter(), 0
    for root, _, fs in os.walk(a):
        for f in fs:
            if not f.endswith(".dcm"):
                continue
            pa = os.path.join(root, f)
            pb = os.path.join(b, os.path.relpath(pa, a))
            if not os.path.exists(pb):
                diff[("<file>", "missing in second output")] += 1
                continue
            n += 1
            A, B = flat(pydicom.dcmread(pa)), flat(pydicom.dcmread(pb))
            for k in set(A) | set(B):
                if A.get(k) != B.get(k):
                    kind = "only_first" if k not in B else "only_second" if k not in A else "value"
                    diff[(k.split("[")[0], kind)] += 1
    print("files compared:", n)
    for (k, kind), c in sorted(diff.items()):
        print(f"{c:8d}  {k}  {kind}")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
