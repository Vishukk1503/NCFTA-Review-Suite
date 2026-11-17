# NCFTA Review Suite

Local v3 field extraction with a coloured terminal dashboard and Excel review. Supports NCFTA groups **6, 7 and 8** and saved JSON. This is separate from NCFTA-MISP-CLI.

## Start here: Work PC setup

**[Download the complete v3.2.1 Windows bundle (1.38 GB)](https://github.com/Vishukk1503/NCFTA-Review-Suite/releases/download/v3.2.1/NCFTA-Review-Suite-Windows-v3.zip)**

Filename: **NCFTA-Review-Suite-Windows-v3.zip**. It is uploaded under **[Releases → v3.2.1 → Assets](https://github.com/Vishukk1503/NCFTA-Review-Suite/releases/tag/v3.2.1)**. This complete bundle already includes the Intel GPU device-name fix, the trained v3 model, the FP32 Intel encoder, the application and the BAT installer. **One ZIP is sufficient; no separate patch is needed for this bundle.** This repository and its release assets are public; anyone can download the bundle, including the model.

**For a fresh installation, use this complete bundle.** GitHub's **Code → Download ZIP**, **Source code (zip)** and the small **BAT-Update.zip** do not include the model. If setup reports `Missing file: assets/model-v3/model.safetensors`, download and extract the complete bundle linked above.

1. Download **NCFTA-Review-Suite-Windows-v3.zip** using the link above.
2. Right-click the ZIP → **Extract All** into a short folder, for example `C:\Users\YourName\NCFTA`, then open the extracted **NCFTA-Review-Suite** folder. Avoid placing it inside several old download/repository folders: long paths can make package installation fail. Allow about 6 GB free for the model, environment and package downloads, plus space for saved runs.
3. Double-click **Install-Work-PC.bat** inside that folder and wait for **SETUP COMPLETE**. It calls Python directly. The default profile targets Intel CPU + Arc/UHD GPU, including Core Ultra 9 285H / Arc 140T. The complete v3.2.1 bundle includes both the BAT installer and the Intel GPU device-name fix.
4. Put your NCFTA API key in `.env`. The installer preserves an existing file and never prints the key.
5. Double-click **Start-NCFTA.bat**. Select dates or saved JSON, groups (`all` means 6, 7, 8) and processing device. Choose **intel-gpu** for Intel acceleration.

**Updating an existing full installation:** download the **[BAT update (24 KB)](https://github.com/Vishukk1503/NCFTA-Review-Suite/releases/download/v3.2.0/NCFTA-Review-Suite-BAT-Update.zip)**, extract all its files into the existing suite folder, replace the included files, and rerun **Install-Work-PC.bat**. This update requires the model files from a complete installation.

**Existing v3.2.0 installation with `Unexpected encoder execution device: ['GPU.0']`:** the optional **[small Intel GPU fix](https://github.com/Vishukk1503/NCFTA-Review-Suite/releases/download/v3.2.1/NCFTA-Intel-GPU-Fix.zip)** replaces only `intel_backend.py` and `BUNDLE-SHA256.json`. Copy both beside **Install-Work-PC.bat**, replace the files, then rerun that installer. Keep the model, `.venv` and settings. This small patch is for the older v3.2.0 bundle; the complete v3.2.1 download above already contains the fix. For older v3.1 installations, apply the BAT update first and this Intel fix afterward, or use the complete v3.2.1 bundle.

The BAT discovers Python 3.12 x64 or installs it through WinGet. The Python setup runner creates a local `.venv`, installs the pinned packages, and checks the model. It can install a missing Microsoft Visual C++ x64 runtime through WinGet; Windows/IT approval may be required. Packages come from PyPI and the official PyTorch index. No PowerShell is launched or required. Setup never fetches a different model, retrains, or contacts NCFTA.

Excel uses **openpyxl**: Microsoft Excel, Node and Codex are not needed to create workbooks. The older optional Node exporter remains for existing configured installations.

### If setup does not start

The BAT immediately prints **NCFTA Review Suite - BAT setup v3.2**, then **Python installer started**. The Python runner prints each stage, streams command output/errors, and saves a log under `setup/install-*.log` with a corresponding JSON status file. If the suite folder cannot hold a log, the runner uses the Windows temporary folder and displays its exact path.

**SETUP COMPLETE** is printed after package checks, Excel export and the model check pass. **SETUP FAILED** includes the failed stage and command/error. The BAT returns Python's exit code directly. **Press any key to CLOSE this window** closes the window after the result is shown.

For an older installation, apply **NCFTA-Review-Suite-BAT-Update.zip** as a complete update, including `install_work_pc.py`. Run the BAT outside the ZIP. The update does not contain model files; retain the full Windows bundle. Earlier `.ps1` files may remain in an older folder, but the new launchers never call them. The full v3.2 bundle excludes those scripts.

All application launchers stay in the current window and preserve failure exit codes. `Start-NCFTA.cmd` is an alias for `Start-NCFTA.bat`. `Check-Intel-GPU.bat` forwards options such as `--help`, `--run` and `--samples` to the Intel check. `Open-Driver-Help.bat --list` prints the driver-help links without opening a browser. BAT files keep the window open until a key is pressed.

### Packages

| Package | Version |
| --- | --- |
| PyTorch | 2.14.1+cpu (Intel/CPU); 2.14.1+cu126 (NVIDIA) |
| OpenVINO | 2026.4.1 |
| Transformers / tokenizers | 5.22.2 / 0.23.3 |
| huggingface_hub / safetensors | 2.4.0 / 0.8.0 |
| sentencepiece / NumPy / Pydantic | 0.2.3 / 2.5.4 / 2.14.3 |
| Rich / requests / PyMISP | 15.0.0 / 2.34.2 / 2.5.34.4 |
| psutil / openpyxl | 7.2.2 / 3.1.5 |
| GLiNER2 | Vendored checksum-verified 2.1.0; not pip-upgraded |

`requirements-work-pc.txt` pins application packages and `constraints-windows.txt` pins their supporting dependencies. The installer uses pip 26.3. `requirements-tested-windows.txt` records the verified Intel/CPU environment; `setup/installed-packages.txt` records the actual work PC installation. `setup/hardware-report.json` records discovery, integrity and synthetic smoke results. Installation stops on package, `pip check` or smoke failures.

### GPU, CPU and NPU

| Device | v3 execution |
| --- | --- |
| Intel Arc / UHD | OpenVINO FP32 encoder; original FP32 head on CPU; select `intel-gpu` |
| NVIDIA | Original PyTorch CUDA path; install NVIDIA profile |
| CPU | Original PyTorch FP32 path |
| Intel AI Boost NPU | Detected and reported, but disabled for strict FP32 v3 |

The NPU accepts F32 model inputs but its hardware computation is FP16. Installing its driver does not make it a valid FP32 backend. [Intel NPU documentation](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html).

Drivers are separate from Python packages. Use the PC manufacturer's or IT-approved Intel graphics/NPU drivers. **Open-Driver-Help.bat** opens the official instructions without replacing drivers. If OpenVINO cannot see an Intel GPU, the Intel setup check fails explicitly; the CPU profile remains available. GPU and NPU are not combined.

`auto` retains CUDA-then-CPU behavior. Intel remains an explicit choice pending full validation on the target PC. Setup checks one synthetic input on CPU and the requested accelerator, checking actual execution and exact output fields/text/positions. This establishes installation health, not accuracy. Menu **7** compares up to 20 saved inputs on CPU and Intel GPU.

```bat
Install-Work-PC.bat --profile intel
Install-Work-PC.bat --profile nvidia
Install-Work-PC.bat --profile cpu
Install-Work-PC.bat --plan
Install-Work-PC.bat --python "C:\Python312\python.exe"
Install-Work-PC.bat --wheelhouse "D:\approved-wheels"
Install-Work-PC.bat --venv "D:\NCFTA-environment"
```

An optional wheelhouse must include every Windows/Python 3.12 dependency and the selected Torch build. Offline setup requires Python and Visual C++ already installed; no wheelhouse is included. `--skip-model-check` checks packages without establishing model readiness. The default performs the model check. Legacy option spellings such as `-Profile`, `-VenvPath` and `-Plan` are also accepted. An explicit `--python` or `NCFTA_PYTHON` must point to a working Python 3.12 x64 executable.

## Workflow

Date range + groups → read-only MISP events → objects/direct attributes → original source context → v3 extraction → combined Excel and group-6 / group-7 / group-8 folders.

- Dates are inclusive **MISP event dates**, not necessarily crime or modification dates. Interactive input accepts `YYYY-MM-DD` or `MM/DD/YYYY`.
- Server permissions control group access; an empty group does not prove permission was denied.
- PyMISP is preferred; requests is explicitly reported if PyMISP is unavailable. Server/access failures do not cause a silent fallback.
- All **118 field meanings** remain unchanged. **116 extraction fields** are queried; two observation timestamps stay metadata-only.
- FP32, threshold **0.50**, TF32 disabled, original source formatter/tokenizer/architecture/checkpoint unchanged.
- Only exact spans inside the source value are accepted. Rejected suggestions and missing-field warnings stay visible; warnings never manufacture output.
- UUID review IDs and event/object/attribute relationships are preserved. Full-input duplicates can reuse predictions within a run; identical values with different context cannot.
- No remote writes, Groq calls, training, model replacement or test-set access. Attachment binaries are not fetched.

## CLI

Menu: **1** fetch/extract; **2** saved JSON; **3** resume; **4** export; **5** setup; **6** output folder; **7** Intel comparison; **0** exit.

```bat
Start-NCFTA.bat doctor
Start-NCFTA.bat run --from-date 2026-11-09 --to-date 2026-11-15 --groups 6 7 8 --device intel-gpu
Start-NCFTA.bat import --json "C:\data\events.json" --device intel-gpu
Start-NCFTA.bat resume --run "C:\data\run-folder" --device cpu
Start-NCFTA.bat export --run "C:\data\run-folder"
Check-Intel-GPU.bat --run "C:\data\saved-run" --samples 20
```

Default CPU threads: 4. `--max-new 100` pauses after 100 new attributes; resume continues. `infer` extracts without export. `NCFTA_PYTHON` can override the local `.venv` runtime.

## Excel and saved progress

Each run saves raw pages, events, records, predictions, logs, manifests and timestamped workbooks under `runs/`. There is a combined workbook and separate group folders; empty groups receive `NO-DATA.txt`. Existing workbooks/reviewer edits are never overwritten.

- **Summary:** dates, groups, counts, precision, device, inference time, model/input hashes.
- **Review:** source context, v3 suggestions, warnings and editable decisions/corrections/notes.
- **Predictions:** accepted/rejected fields, confidence and exact offsets.
- **Source text:** full input in numbered chunks. XML-incompatible control characters are replaced visibly in Excel; JSON is authoritative.

Identifiers retain leading zeros. Formula-like text is escaped. Long cells have labelled previews, with complete data retained in JSON. Confidence is not calibrated accuracy; F1 is unavailable without independently reviewed labels. Review corrections are not automatically imported or used for training.

Resume checks asset/input hashes and skips completed records. Interrupted fetches recheck saved pages and stop if contents changed. Saved data is not a transactional snapshot of a changing MISP server.

## Integrity and limitations

V3 SHA-256: `ac95258b1c2baaffd6a7dabbe5f8433c243d1b968bba693bddf6a6296db41380`.

`assets/checksums.json` verifies checkpoint, tokenizer, formatter, catalog and vendored code. The Intel backend also verifies encoder hashes, actual execution device and FP32 computation, stopping on compilation/precision failure.

On the development PC's Intel UHD GPU, an earlier 20-input comparison matched CPU fields/text/positions. Inference was 61.3 seconds CPU versus 17.3 seconds UHD; including loading/compilation it was 72.6 versus 53.0 seconds. These are sample measurements, not Arc 140T results or full validation. The work laptop has not been accessed or tested.

Known model errors remain, including missed routing numbers and target organizations. Ambiguous `other` values need review. Packaging changes do not improve accuracy.

## Development and distribution

This public Git repository contains code and small verified assets. Large checkpoint/encoder files are in the release ZIP. Credentials, NCFTA data, predictions, datasets, run folders and personal settings are excluded from both. Verify the release ZIP using `certutil -hashfile NCFTA-Review-Suite-Windows-v3.zip SHA256` against `SHA256SUMS.txt`.

The read-only `ncfta_client.py` originates from `Vishukk1503/NCFTA-MISP-CLI`, blob `295c16baac2cc635a322c7ec3c704d5703dc8040`. That repository is unchanged. Vendored GLiNER2's Apache 2.0 license is in `assets/GLINER2-LICENSE.txt`.

```bat
.\.venv\Scripts\python.exe -m unittest discover -p 'test_*.py' -v
```

Tests use synthetic records and mocked network calls. Setup smoke and saved-input comparisons are separate; the frozen test remains untouched.

The Windows launcher and setup tests cover paths with spaces/ampersands, argument forwarding, missing files/runtime, visible child-process failures, offline package installation, and preservation of existing settings and credentials. Tests check that BAT/CMD launchers and the Python setup runner have no PowerShell dependency. Installation readiness is checked separately by the real installer and synthetic CPU/Intel FP32 comparison; it does not establish Arc 140T performance or full validation accuracy. Python and Visual C++ are already installed on the development PC, so first-time prerequisite installation still needs verification on a clean machine.
