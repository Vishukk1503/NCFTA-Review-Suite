# Install the Snowflake loader update

For the simpler complete download, use the v3.3.0 Windows release asset and [START-HERE.md](../START-HERE.md). That bundle already includes this loader; the patch below is only for older installations.

This small update is for an existing NCFTA Review Suite Windows v3.2.1 installation. It contains application code and SQL, without model weights, credentials, local settings or extraction data.

1. Close the suite and back up its folder.
2. Extract `NCFTA-Snowflake-Loader-Update.zip` into the existing suite folder containing `suite.py` and `Start-NCFTA.bat`. Replace the matching code files and merge the `snowflake` folder. Do not run from inside the ZIP or create an extra nested suite folder.
3. The tables and procedures from scripts 01–07 must already be deployed and verified. If you deployed the current scripts, you do not need to recreate them.
4. Use the existing Snowflake CLI `snow` and connection profile `cdp_prd` on your work PC. If your profile has another name, copy `snowflake-loader.example.json` to `snowflake-loader.json` and change `connection`. An optional `warehouse` setting can select your warehouse. Do not place passwords or API keys in this file.
5. Open `Start-NCFTA.bat`, choose **8 — Load into Snowflake**, and select one small completed extraction run. Complete browser sign-in when prompted. The loader waits, then uploads saved JSON and calls RAW → REFINED → APP.
6. Check the displayed statuses and counts. Use the displayed audit run ID to find the run in `AUDIT_LAYER.NCFTA_LOAD_AUDIT`. Reports are also saved under the selected run's `snowflake-loads` folder.

The input is the completed parent run with its manifest, events, attribute records, predictions and Excel export. The loader reads the saved JSON rather than extracting data from the workbook. Existing `runs`, `.env`, `settings.json`, model files and Python environment are not included in this update.

To prepare a package locally before connecting, run from the suite folder:

```powershell
.venv\Scripts\python.exe suite.py snowflake --run "runs\YOUR-COMPLETED-RUN" --package-only
```

The update also includes source-deletion handling and the dashboard NCFTA connection check. If resuming an incomplete fetch created with the old deletion policy is rejected, start a new extraction run; existing saved data remains intact.

104 automated tests passed before publication. Live browser login and Snowflake execution still need validation on your work PC. Use one loader at a time. If interrupted after submitting a query, check the audit before retrying; server work can still be running. The full v3.2.1 Windows release bundle has not been rebuilt; this ZIP is its code update.
