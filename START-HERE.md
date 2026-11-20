# One download: NCFTA Review Suite v3.3.0

Download the `NCFTA-Review-Suite-Windows-v3.3.0.zip` asset from the v3.3.0 GitHub release. Do not choose GitHub's automatic Source code ZIP; that does not include model weights.

The full bundle includes extraction, Excel export, the Snowflake loader (menu 8), model assets, connection diagnostics and Snowflake SQL. No separate patches are needed.

## Updating your existing suite

1. Close the suite and back up its folder.
2. Extract the ZIP. Copy the contents of its `NCFTA-Review-Suite` folder into your existing suite folder containing `Start-NCFTA.bat`. Replace matching files and merge folders.
3. Your `.env`, `settings.json`, `.venv` and `runs` are not shipped in the ZIP, so they stay in place.
4. Open `Start-NCFTA.bat`. Choose **8 — Load into Snowflake**, select a completed run, and finish browser sign-in when prompted.

If you already installed and verified SQL scripts 01–07, do not recreate the tables. The loader calls those procedures after connecting.

## New installation

Extract the ZIP, run `Install-Work-PC.bat`, configure your NCFTA credentials following README.md, then open `Start-NCFTA.bat`. Installation may download Python dependencies; this is not a fully offline installer.

Snowflake loading uses your installed Snowflake CLI `snow` and existing `cdp_prd` connection profile. The Snowflake CLI is not bundled. A different profile/warehouse can be set in `snowflake-loader.json` using the example provided. The database tables/procedures must be deployed first (see `snowflake/DEPLOYMENT.md`).

Menu 8 loads a completed run's saved JSON and predictions, without rerunning the model. RAW → REFINED → APP counts and the audit run ID appear at the end. Start with one small run. Automated checks passed; live Snowflake execution still needs verification in your work environment.
