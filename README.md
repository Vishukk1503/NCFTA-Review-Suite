# NCFTA Review Suite

Local v3 field extraction with a coloured terminal dashboard and Excel review. Supports NCFTA groups **6, 7 and 8** and saved JSON. This is separate from NCFTA-MISP-CLI.

## Work PC setup

1. Download **NCFTA-Review-Suite-Windows-v3.zip** from [this private repository's releases](https://github.com/Vishukk1503/NCFTA-Review-Suite/releases). Use the named asset: GitHub's generated Source code ZIP excludes model weights.
2. Extract to a writable folder. Allow about 6 GB free for the model, environment and package downloads, plus space for saved runs.
3. Double-click **Install-Work-PC.bat**. The default profile targets Intel CPU + Arc/UHD GPU, including Core Ultra 9 285H / Arc 140T.
4. Put your NCFTA API key in `.env`. The installer preserves an existing file and never prints the key.
5. Double-click **Start-NCFTA.bat**. Select dates or saved JSON, groups (`all` means 6, 7, 8) and processing device. Choose **intel-gpu** for Intel acceleration.

The installer creates a local Python 3.12 x64 `.venv`. It can install missing Python through WinGet and the Microsoft Visual C++ x64 runtime if needed; Windows/IT approval may be required for the latter. It installs packages from PyPI and the official PyTorch index. It never fetches a different model, retrains, or contacts NCFTA during setup.

Excel uses **openpyxl**: Microsoft Excel, Node and Codex are not needed to create workbooks. The older optional Node exporter remains for existing configured installations.

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

```powershell
.\Install-Work-PC.bat -Profile intel
.\Install-Work-PC.bat -Profile nvidia
.\Install-Work-PC.bat -Profile cpu
.\Install-Work-PC.bat -Plan
.\Install-Work-PC.bat -Python "C:\Python312\python.exe"
.\Install-Work-PC.bat -Wheelhouse "D:\approved-wheels"
```

An optional wheelhouse must include every Windows/Python 3.12 dependency and the selected Torch build. Offline setup requires Python and Visual C++ already installed; no wheelhouse is included. `-SkipModelCheck` checks packages without establishing model readiness. The default performs the model check.

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

```powershell
.\Start-NCFTA.ps1 doctor
.\Start-NCFTA.ps1 run --from-date 2026-11-09 --to-date 2026-11-15 --groups 6 7 8 --device intel-gpu
.\Start-NCFTA.ps1 import --json "C:\data\events.json" --device intel-gpu
.\Start-NCFTA.ps1 resume --run "C:\data\run-folder" --device cpu
.\Start-NCFTA.ps1 export --run "C:\data\run-folder"
.\Start-NCFTA.ps1 check-intel --run "C:\data\saved-run" --samples 20
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

This private Git repository contains code and small verified assets. Large checkpoint/encoder files are in the private release ZIP. Credentials, NCFTA data, predictions, datasets, run folders and personal settings are excluded from both. Verify the release ZIP using `Get-FileHash -Algorithm SHA256` against `SHA256SUMS.txt`.

The read-only `ncfta_client.py` originates from `Vishukk1503/NCFTA-MISP-CLI`, blob `295c16baac2cc635a322c7ec3c704d5703dc8040`. That repository is unchanged. Vendored GLiNER2's Apache 2.0 license is in `assets/GLINER2-LICENSE.txt`.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p 'test_*.py' -v
```

Tests use synthetic records and mocked network calls. Setup smoke and saved-input comparisons are separate; the frozen test remains untouched.

Release verification: 22 automated tests passed in a fresh Windows Python 3.12 environment. The PowerShell installer completed with `pip check`, portable Excel export, full asset checks and a one-input CPU/Intel UHD GPU agreement check using an empty model cache. Intel execution was confirmed as FP32. This does not establish Arc 140T performance or full validation accuracy.
