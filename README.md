# CapaSim — Capacity Simulation (MES · Pharma)

Web interface for launching and reviewing capacity-planning simulations that run in **Microsoft Fabric**. A small Flask backend talks to Fabric and OneLake; the front end is a single HTML page with no build step.

## What it does

- Lists simulation scenarios stored in the `Simulation` Delta table of the Planning lakehouse (newest first), grouped by cycle.
- Creates a new simulation by sending its parameters to a Fabric notebook and polling the job until it finishes.
- Tracks scenario status: **Lancé** → **En cours** → **Validé** → **Archivé** (persisted back to the Delta table).
- Downloads each simulation's Excel result file from OneLake.
- Compares 2–4 scenarios side by side, with a superimposed load chart.
- Supports three capacity modes: **Finie**, **Infinie**, **Matière**.
- Light and dark themes in the Sothema colors.

## Project structure

```
.
├── app.py                 # Flask backend (Fabric / OneLake access)
├── templates/
│   └── index.html         # Front end (HTML + CSS + JS in one file)
├── .env                   # Configuration (not committed)
└── README.md
```

## Requirements

- Python 3.10+
- Access to the Microsoft Fabric workspace, notebook and lakehouse
- A browser for the Microsoft sign-in popup (interactive login)

Python packages:

```bash
pip install flask python-dotenv azure-identity requests deltalake pyarrow
```

## Configuration

Create a `.env` file next to `app.py`:

```env
FLASK_SECRET_KEY=change-me
FLASK_PORT=5000

FABRIC_TENANT_ID=<tenant id>
FABRIC_WORKSPACE_ID=<workspace id>
FABRIC_NOTEBOOK_ID=<simulation notebook id>
FABRIC_LAKEHOUSE_ID=<Planning_Lakehouse id>

# Optional: path of the Simulation table inside the lakehouse.
# If empty, the app tries "Tables/Simulation" then "Tables/dbo/Simulation".
FABRIC_SIMULATION_TABLE_PATH=
```

## Run

```bash
python app.py
```

Then open <http://127.0.0.1:5000>. The first call to Fabric opens a Microsoft sign-in window; the credential is then reused until the server restarts.

## API

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Serves the interface |
| GET | `/api/simulations` | Lists scenarios from the `Simulation` table |
| POST | `/api/simulations/<sim_id>/status` | Updates a scenario's status (`brouillon`, `encours`, `valide`, `archive`) |
| GET | `/api/simulations/excel-files` | IDs of simulations that have an Excel file |
| GET | `/api/simulations/<sim_id>/excel` | Downloads `simulation_<ID>.xlsx` from OneLake |
| POST | `/api/run-simulation` | Starts the Fabric notebook with the simulation parameters |
| GET | `/api/job-status/<job_id>` | Status of a running notebook job |
| GET | `/api/test-fabric` | Checks the connection to the Fabric workspace |

Simulation IDs must match `SIM<number>`. Excel files are read from `Files/simulation_excel/` in the lakehouse.

## Simulation parameters

`/api/run-simulation` expects all of these keys in the JSON body, and returns `400` if one is missing:

`CAPACITE_MODE`, `MODE_PLANIF`, `SIM_DESC`, `SIM_USER`, `SIM_DIVISIONS`, `SIM_LAUNCH_DATE`, `SIM_ARRETS_JSON`, `VERIF_STOCK`, `VERIF_ECART_OPS`, `VERIF_FENETRE`, `HORIZON_MP_CRITIQUE_JOURS`, `HORIZON_H_DAYS`, `ADVANCE_RETREAT_DAYS`, `MAX_GAP_BETWEEN_OPS_HOURS`, `HORIZON_DEB`, `HORIZON_FIN`, `LOG_LEVEL`, `LOG_FILTER`

The four `*_DAYS` / `*_HOURS` values are sent to the notebook as numbers; everything else as text.

## Theming

- The theme toggle (sun/moon button in the header) switches between light and dark. The choice is saved in the browser (`localStorage`, key `capasim-theme`); on first visit it follows the operating system setting.
- All colors are CSS variables at the top of the `<style>` block in `index.html`: `:root` holds the light theme and `:root[data-theme="dark"]` the dark one. To change the palette, edit those two blocks.
- Brand colors: navy `#243C7C`, cyan `#0E9AD7`, red `#E4202B`.
- Badge colors for capacity modes are set in `capMeta()` in the script: Finie = red, Infinie = cyan, Matière = green.

## Notes

- Running jobs are tracked in memory (`job_locations`). Restarting the server loses track of jobs already started; they still finish in Fabric.
- The server binds to `127.0.0.1` only and runs with `debug=False`. It is meant for local or internal use, not for direct exposure to the internet.
- The Delta table is opened directly in OneLake; column names are read case-insensitively.
