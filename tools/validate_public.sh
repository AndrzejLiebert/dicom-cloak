#!/usr/bin/env bash
# Real-data validation runbook (public TCIA collections or other data you may process).
#   tools/validate_public.sh IN WORKROOT [threads]
# IN: one sub-folder per study/patient (symlink trees are fine: cp -rs SRC IN/<label>).
# Runs, in WORKROOT: CTP engine -> checker -> Python engine -> checker -> engine comparison ->
# dcm2niix before/after per input folder. Run inside tmux; needs DAT_HOME + JAVA for the CTP part,
# dcm2niix + nibabel for the comparison. Uses --use-existing-patient-id (TCIA PatientIDs are already
# pseudonyms). Delete WORKROOT afterwards (tens of GB).
set -u
IN=${1:?input folder}; W=${2:?work root}; T=${3:-8}
HERE=$(cd "$(dirname "$0")/.." && pwd); CFG=$HERE/config/config.yaml
mkdir -p "$W"
date
dicom-cloak-ctp run --src "$IN" --dst "$W/out_ctp" --work "$W/work" --use-existing-patient-id \
  --uid-key "$W/key.hex" --config "$CFG" --threads "$T" --workers "$T" > "$W/ctp.log" 2>&1; echo "CTP_EXIT=$?" | tee -a "$W/ctp.log"
dicom-cloak-check "$W/out_ctp" --config "$CFG" --require-remapped-uids > "$W/check_ctp.log" 2>&1; echo "CHECK_CTP_EXIT=$?"
dicom-cloak run --src "$IN" --dst "$W/out_py" --use-existing-patient-id --uid-key "$W/key.hex" \
  --config "$CFG" --workers "$T" > "$W/py.log" 2>&1; echo "PY_EXIT=$?" | tee -a "$W/py.log"
dicom-cloak-check "$W/out_py" --config "$CFG" --require-remapped-uids > "$W/check_py.log" 2>&1; echo "CHECK_PY_EXIT=$?"
python "$HERE/tools/compare_engines.py" "$W/out_py" "$W/out_ctp" > "$W/engines.log" 2>&1; echo "ENGINES_EXIT=$?"
for d in $(ls "$IN"); do
  f=$(find -L "$IN/$d" -type f | head -1)
  pid=$(python -c "import pydicom,sys; print(pydicom.dcmread(sys.argv[1],stop_before_pixels=True).PatientID)" "$f")
  echo "== $d -> $pid"
  dicom-cloak-compare "$IN/$d" "$W/out_ctp/$pid" --config "$CFG" 2>&1 | tail -8
done > "$W/compare.log"
grep -c '^PASS' "$W/compare.log" | sed 's/^/dcm2niix PASS: /'; grep -c '^FAIL' "$W/compare.log" | sed 's/^/dcm2niix FAIL: /'
date
