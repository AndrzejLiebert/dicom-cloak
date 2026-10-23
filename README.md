# DICOM Cloak

**A MAGIC Lab tool for de-identifying breast MRI DICOM data for research**
*(MAGIC Lab: Medical AI and Generative Image Computing, IPI PAN)*

DICOM Cloak puts an invisibility cloak over the patient: it makes a **de-identified copy** of MRI
DICOM data (Siemens, Philips, GE) that you can share for research, while the images underneath stay
exactly as they were. Your original data is only read, never changed.

What it does to the copy:

| In the original | In the copy |
|---|---|
| Patient name and patient ID | a code (pseudonym) such as `SUBJ_0001` |
| Other IDs, accession number, hospital, department, physicians, station name, device serial number | removed |
| Dates (study, birth, ...) | only the year is kept |
| Times of day | replaced by a relative clock; the exact intervals between scans are kept |
| Patient age | an age group, e.g. `41-50` |
| Contrast phases (DCE) | numbered, with seconds after the pre-contrast scan |
| Hidden vendor fields (private tags) | removed, except those needed for image conversion (e.g. b-values, scaling) |
| Reports, screenshots, presentation states, key images, non-DICOM files | not copied |
| Image pixels | unchanged |

Every written file is checked again automatically. The run ends with **PASS** only if no identifying
information is found.

> **Important:** the program cleans the DICOM *header* (the hidden text information). It cannot read
> text that is burned into the image itself, such as a name in a screenshot or annotated MIP. Always
> look at a few images before you share the data (see step 5).

---

## What you need

- A computer with Windows 10/11, macOS (Apple chip or Intel), or Linux.
- **Docker**: free software that runs this program in a sealed box. It is installed once (step 1). On
  Windows you usually need administrator rights for that, so ask your IT department if needed.
- Internet access the **first time only**, to download components (a few minutes). The data
  processing itself runs **without any network access**.
- Free disk space for the copy: about as much as the original data.

---

## Step 1: Install Docker (once)

**Windows**
1. Download **Docker Desktop for Windows** from https://www.docker.com/products/docker-desktop/ and
   run the installer. Keep the default options ("Use WSL 2").
2. Restart the computer if asked.
3. Start **Docker Desktop** from the Start menu. Accept the terms and skip the sign-in (no account is
   needed). Wait until it shows "Engine running".

**Mac**
1. Find out which chip your Mac has: Apple menu → *About This Mac* ("Apple M1/M2/M3/M4..." or
   "Intel").
2. Download **Docker Desktop for Mac** for that chip from https://www.docker.com/products/docker-desktop/,
   open the downloaded file and drag Docker to *Applications*.
3. Start **Docker** from *Applications*. Accept the terms, skip the sign-in, and wait until it shows
   "Engine running".

**Linux** (Ubuntu and similar): install Docker Engine as described at
https://docs.docker.com/engine/install/, then allow your user to use it:
`sudo usermod -aG docker $USER`, and log out and back in.

> Docker Desktop is free for personal use, education, research and small organisations. Large
> organisations may need a paid subscription, so check with your IT department.

**Docker must be running whenever you use this program** (on Windows and Mac: start Docker Desktop
first).

## Step 2: Download DICOM Cloak

1. Open the DICOM Cloak repository page on GitHub (the link you got from your colleague).
2. Click the green **Code** button → **Download ZIP**.
3. Unzip it to a place where it can stay, e.g. `Documents\dicom-cloak` (Windows) or
   `Documents/dicom-cloak` (Mac). The unzipped folder may be called `dicom-cloak-main`; you can rename
   it. Do not rename or remove the files inside it.

**Updating to a newer version:** download the ZIP again, replace the old program folder with the new
one, and run once with the `--rebuild` option (see "Options"). Your secrets folder (step 6) is stored
elsewhere and is not affected. If you changed `config/config.yaml`, copy your change into the new
folder.

## Step 3: Prepare the folders

- **Input folder**: the folder with the original DICOM files (e.g. a PACS export or a CD/USB copy). It
  may contain any number of patients and sub-folders.
- **Output folder**: a **new or empty** folder for the de-identified copy. It must **not** be inside
  the input folder. Use a new output folder for every run.

If the data is on a network drive, copy it to the computer's own disk first: Docker often cannot
read network drives, and local disks are much faster.

## Step 4: Run the program

**Windows**
1. Make sure Docker Desktop is running.
2. Double-click **`cloak.bat`** in this folder. If Windows shows a security warning, choose *Run*
   (or *More info* → *Run anyway*).
3. When asked, **drag the input folder** from Explorer into the black window and press **Enter**.
4. When asked, drag (or type) the **output folder** and press **Enter**. A folder that does not exist
   yet is created.

**Mac**
1. Make sure Docker Desktop is running.
2. Open **Terminal** (press `Cmd + Space`, type *Terminal*, press Enter).
3. Type `bash ` (the word bash followed by a space), then **drag the file `cloak.sh`** from this folder
   into the Terminal window, and press **Enter**.
4. When asked, **drag the input folder** into the Terminal window and press **Enter**.
5. When asked, drag (or type) the **output folder** and press **Enter**.

**Linux**: in a terminal, run `bash /path/to/this/folder/cloak.sh` and answer the two questions, or give
both folders directly: `bash cloak.sh /data/export /data/export_deid`.

The **first run** builds the program and takes a few minutes; later runs start right away. Progress is
shown per patient and study. At the end you see one of these:

```
 RESULT: PASS       -> the copy is ready (go to step 5)
 RESULT: FAILED     -> do NOT share the output folder (see "Problems" below)
```

## Step 5: Check and share

1. Open the output folder. It looks like this:
   ```
   SUBJ_0001/
     study_01/                 first visit of this patient (study_02 = second visit, ...)
       0003_t1_fl3d_native/    one folder per series (number and series name)
         IM_00001_00001.dcm
   SUBJ_0002/ ...
   _deid_meta/                 overview tables (open with Excel)
     patients.csv              per study: pseudonym, visit, days since first visit, sex, age group, year
     series_timing.csv         per series: role (native / dynamic / subtraction ...), contrast phases, seconds
     excluded.csv              what was not copied, and why
   ```
2. Open a few images of each kind (especially MIPs, subtractions, maps and anything that looks like a
   screenshot) in your viewer and check that **no name, date or ID is visible in the image**.
3. Share **only the output folder**.

## Step 6: Keep the secrets folder safe

The program keeps a private folder named **`dicom_cloak_secrets`** in your user folder
(`C:\Users\<you>\dicom_cloak_secrets` on Windows, `/Users/<you>/dicom_cloak_secrets` on Mac). It
contains:

| File | Purpose |
|---|---|
| `pseudonym_map.csv` | which real patient ID got which pseudonym (the key list) |
| `uid_key.hex` | secret key that makes the replaced DICOM identifiers consistent across runs |
| `study_registry.csv` | visit dates, so visits are numbered correctly when data arrives in batches |
| `logs/` | a log of every run |

- **Never share this folder** and never put it next to the shared data.
- **Back it up.** As long as you use the same secrets folder, a patient keeps the same pseudonym in
  every run, and follow-up visits are numbered `study_02`, `study_03`, ... If it is lost, the link
  between pseudonyms and patients is lost, and new runs start new pseudonyms.
- For a separate research project with its own pseudonyms, use a separate secrets folder (see
  "Options").

---

## Using your own pseudonyms

By default patients get `SUBJ_0001`, `SUBJ_0002`, ... in the order they are first seen. To choose the
codes yourself, edit `pseudonym_map.csv` in the secrets folder **before** the run. Create it if it does
not exist; it is a two-column list:

```
original_patient_id,pseudonym
1234567,BREAST_001
7654321,BREAST_002
```

The first column is the patient ID exactly as in the original DICOM data. Pseudonyms may contain
letters, digits, `_` and `-` only, and every patient needs a different one. In Excel, save it as
*CSV UTF-8*. Patients that are not in the list still get an automatic code.

## Options

Add these after `cloak.bat` or `bash cloak.sh`, before the folders:

| Option | Effect |
|---|---|
| `--prefix BREAST_` | automatic pseudonyms start with this text (`BREAST_0001`, ...) instead of `SUBJ_` |
| `--secrets FOLDER` | use another secrets folder, e.g. one per research project |
| `--ctp` | use the RSNA CTP anonymizer as the engine, for sites that require it (same rules, same checks) |
| `--rebuild` | rebuild the program after you replaced this folder with a newer version |

Other commands:

| Command | Effect |
|---|---|
| `cloak.bat check FOLDER` / `bash cloak.sh check FOLDER` | check an already de-identified folder again |
| `cloak.bat selftest` / `bash cloak.sh selftest` | run the built-in tests (should end with SELFTEST PASSED) |
| `cloak.bat --help` / `bash cloak.sh --help` | short help |

Example (Mac/Linux): `bash cloak.sh --prefix BREAST_ /data/export /data/export_deid`

## Leaving out certain series

To never copy some series (e.g. test scans or series your site marks "do not send"), open
`config/config.yaml` in this folder with a text editor and fill in `exclude_series_regex` with words
that appear in the series description, for example:

```yaml
exclude_series_regex: ['test', 'do not send', 'phantom']
```

Upper and lower case do not matter. Excluded series are listed in `_deid_meta/excluded.csv`. The other
settings in that file are for technical staff.

## Problems

| Message | What to do |
|---|---|
| `Docker is not installed` | Do step 1. |
| `Docker ... is not running` | Start Docker Desktop and wait until it says "Engine running", then try again. |
| `Building the Docker image failed` | The first run needs internet. Check the connection (and proxy settings in Docker Desktop) and try again. |
| `the output folder is not empty` | Choose a new, empty output folder. |
| `... must not be inside ...` | The input, output and secrets folders must be separate; none may be inside another. |
| `input folder not found` | Drag the folder again, or type the full path. |
| `RESULT: FAILED` | Do not share the output. Keep the log mentioned on screen (it is in `logs/` of the secrets folder), and send it to your technical contact. It contains no header values, but it may show input folder names, so treat it as confidential. |
| Very slow on a Mac | Copy the data to the Mac's internal disk first; give Docker more CPUs in Docker Desktop → Settings → Resources. |
| Mac: `permission denied` or folder cannot be read | Docker Desktop → Settings → Resources → File sharing: add the disk or folder. |
| Linux: `permission denied` | Run `sudo usermod -aG docker $USER`, then log out and back in. |

## Privacy notes

- The program runs on your computer only. During processing the container has **no network access**,
  and the input folder is opened **read-only**.
- File names in the output never come from the original file names.
- Header text that is not on the list of technically needed fields is removed, so unknown fields cannot
  leak identifiers.
- De-identification lowers the risk of re-identification but does not remove every risk (for example,
  a rare diagnosis plus an age group). Follow the data-protection rules of your institution and of the
  study.

---

## For technical staff

- `docs/TECHNICAL.md`: installation without Docker, all command-line options, how the engines work,
  output formats, the policy files in `config/`, tests, validation results and known limitations.
- `docs/CTP.md`: the RSNA CTP engine and the CTP server pipeline (`ctp/config.xml`).
- `docs/SOURCES.md`: external references (dcm2niix, DICOM PS3.15, TCIA, CTP) and versions.

Command-line tools inside the image: `dicom-cloak` (de-identify), `dicom-cloak-check` (independent leak
check), `dicom-cloak-compare` (dcm2niix conversion before/after), `dicom-cloak-report` (which attributes
the allowlist drops), `dicom-cloak-ctp` (CTP engine).

## License

DICOM Cloak is released under the MIT License (see `LICENSE`).
Copyright (c) 2026 Andrzej Liebert, MAGIC Lab, Institute of Computer Science, Polish Academy of
Sciences (IPI PAN). Third-party components and their licenses are listed in `THIRD_PARTY_NOTICES.md`.

DICOM Cloak is research software provided "as is", without warranty. It lowers the risk of
re-identification but does not replace your institution's data-protection review.
