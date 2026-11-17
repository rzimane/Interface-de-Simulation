from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from dotenv import load_dotenv
from azure.identity import InteractiveBrowserCredential
import requests
import os
import re
import json
from datetime import datetime

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Microsoft Fabric configuration
TENANT_ID = os.getenv("FABRIC_TENANT_ID")
WORKSPACE_ID = os.getenv("FABRIC_WORKSPACE_ID")
NOTEBOOK_ID = os.getenv("FABRIC_NOTEBOOK_ID")
LAKEHOUSE_ID = os.getenv("FABRIC_LAKEHOUSE_ID")  # Planning_Lakehouse
# Chemin de la table Simulation dans le lakehouse (selon lakehouse avec ou sans schémas)
SIMULATION_TABLE_PATH = os.getenv("FABRIC_SIMULATION_TABLE_PATH")

FABRIC_API = "https://api.fabric.microsoft.com/v1"

# Authentication will be initialized when needed
credential = None
job_locations = {}
_simulation_table_path = None  # chemin résolu une fois trouvé

# Statut Fabric (colonne Statut de la table Simulation) <-> statut de l'interface
STATUT_TO_UI = {
    "Lancé": "brouillon",
    "En cours": "encours",
    "Validé": "valide",
    "Archivé": "archive",
}
UI_TO_STATUT = {v: k for k, v in STATUT_TO_UI.items()}


def get_token(scope):
    global credential
    if credential is None:
        credential = InteractiveBrowserCredential(tenant_id=TENANT_ID)
    return credential.get_token(scope).token


def open_simulation_table():
    """Ouvre la table Delta Simulation directement dans OneLake (lecture/écriture)."""
    from deltalake import DeltaTable

    global _simulation_table_path

    if not (WORKSPACE_ID and LAKEHOUSE_ID):
        raise RuntimeError(
            "FABRIC_WORKSPACE_ID et FABRIC_LAKEHOUSE_ID doivent être définis dans .env."
        )

    storage_options = {
        "bearer_token": get_token("https://storage.azure.com/.default"),
        "use_fabric_endpoint": "true",
    }

    candidates = (
        [_simulation_table_path]
        if _simulation_table_path
        else [p for p in (SIMULATION_TABLE_PATH, "Tables/Simulation", "Tables/dbo/Simulation") if p]
    )

    last_error = None
    for path in candidates:
        uri = f"abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/{LAKEHOUSE_ID}/{path}"
        try:
            table = DeltaTable(uri, storage_options=storage_options)
            _simulation_table_path = path
            return table
        except Exception as e:
            last_error = e

    raise RuntimeError(f"Table Simulation introuvable dans le lakehouse : {last_error}")


def simulation_row_to_json(raw_row):
    # Noms de colonnes insensibles à la casse (Division / DIVISION / division...)
    row = {str(k).lower(): v for k, v in raw_row.items()}

    created = row.get("credate")
    if isinstance(created, datetime):
        created = created.isoformat()

    divisions = [d.strip() for d in str(row.get("division") or "ALL").split(",") if d.strip()]
    capacite = str(row.get("capacite") or "Finie").lower()
    mode = str(row.get("mode") or "Plutot")

    # Paramètres complets du run (colonne Params, absente des anciennes simulations)
    try:
        params = json.loads(row.get("params") or "{}")
    except Exception:
        params = {}
    if not isinstance(params, dict):
        params = {}

    return {
        "id": row.get("id_sim"),
        "name": row.get("desc_sim") or row.get("id_sim"),
        "user": row.get("user"),
        "created": created,
        "divisions": divisions,                      # ["ALL"] = toutes les divisions
        "capacityMode": capacite,                    # finie | infinie | matiere
        "positioning": "auplustard" if mode == "Plutard" else "auplustot",
        "horizonDeb": row.get("horizondeb") or "",
        "horizonFin": row.get("horizonfin") or "",
        "status": STATUT_TO_UI.get(row.get("statut"), "brouillon"),
        "params": params,
    }


@app.route("/")
def home():
    return render_template("index.html")



@app.route("/api/simulations")
def list_simulations():
    """Liste les scénarios depuis la table Simulation de Fabric (plus récents d'abord)."""
    try:
        rows = open_simulation_table().to_pyarrow_table().to_pylist()
        rows.sort(key=lambda r: r.get("CREDATE") or datetime.min, reverse=True)

        return jsonify({
            "success": True,
            "simulations": [simulation_row_to_json(r) for r in rows]
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


@app.route("/api/simulations/<sim_id>/status", methods=["POST"])
def update_simulation_status(sim_id):
    """Persiste le changement de statut d'un scénario dans la table Simulation."""
    try:
        if not re.fullmatch(r"SIM\d+", sim_id):
            return jsonify({"success": False, "message": "ID de simulation invalide."}), 400

        data = request.get_json(silent=True) or {}
        statut = UI_TO_STATUT.get(data.get("status"))

        if not statut:
            return jsonify({"success": False, "message": "Statut invalide."}), 400

        open_simulation_table().update(
            predicate=f"ID_SIM = '{sim_id}'",
            updates={"Statut": f"'{statut}'"}
        )

        return jsonify({"success": True})

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
EXCEL_DIR = "Files/simulation_excel"   # écrit par le notebook : simulation_<ID_SIM>.xlsx


def onelake_headers():
    return {
        "Authorization": "Bearer " + get_token("https://storage.azure.com/.default"),
        "x-ms-version": "2021-06-08",
    }


@app.route("/api/simulations/excel-files")
def list_excel_files():
    """IDs des simulations dont le fichier Excel existe dans Files/simulation_excel."""
    try:
        ids, continuation = set(), None

        while True:
            params = {
                "resource": "filesystem",
                "recursive": "false",
                "directory": f"{LAKEHOUSE_ID}/{EXCEL_DIR}",
            }
            if continuation:
                params["continuation"] = continuation

            r = requests.get(f"{ONELAKE}/{WORKSPACE_ID}", headers=onelake_headers(),
                             params=params, timeout=30)

            if r.status_code == 404:      # dossier pas encore créé : aucun fichier
                break
            r.raise_for_status()

            for p in r.json().get("paths", []):
                m = re.fullmatch(r"simulation_(SIM\d+)\.xlsx", p["name"].rsplit("/", 1)[-1])
                if m:
                    ids.add(m.group(1))

            continuation = r.headers.get("x-ms-continuation")
            if not continuation:
                break

        return jsonify({"success": True, "ids": sorted(ids)})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/simulations/<sim_id>/excel")
def download_simulation_excel(sim_id):
    """Télécharge simulation_<ID_SIM>.xlsx depuis OneLake (Files/simulation_excel)."""
    try:
        if not re.fullmatch(r"SIM\d+", sim_id):
            return jsonify({"success": False, "message": "ID de simulation invalide."}), 400

        filename = f"simulation_{sim_id}.xlsx"
        url = f"{ONELAKE}/{WORKSPACE_ID}/{LAKEHOUSE_ID}/{EXCEL_DIR}/{filename}"

        upstream = requests.get(url, headers=onelake_headers(), stream=True, timeout=120)

        if upstream.status_code == 404:
            upstream.close()
            return jsonify({"success": False,
                            "message": "Aucun fichier Excel pour cette simulation."}), 404

        if upstream.status_code != 200:
            detail = upstream.text[:300]
            upstream.close()
            return jsonify({"success": False,
                            "message": f"OneLake {upstream.status_code} : {detail}"}), 502

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=65536)),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/test-fabric")
def test_fabric():
    try:
        global credential

        if credential is None:
            credential = InteractiveBrowserCredential(
                tenant_id=TENANT_ID
            )

        token = credential.get_token(
            "https://api.fabric.microsoft.com/.default"
        )

        response = requests.get(
            f"{FABRIC_API}/workspaces/{WORKSPACE_ID}",
            headers={
                "Authorization": f"Bearer {token.token}"
            },
            timeout=30
        )

        if response.status_code == 200:
            return jsonify({
                "success": True,
                "message": "Connexion à Microsoft Fabric réussie !"
            })

        return jsonify({
            "success": False,
            "status": response.status_code,
            "message": response.text
        }), response.status_code

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500



@app.route("/api/run-simulation", methods=["POST"])
def run_simulation():
    try:
        global credential

        data = request.get_json()

        if not isinstance(data, dict):
            return jsonify({
                "success": False,
                "message": "Paramètres invalides."
            }), 400

        if credential is None:
            credential = InteractiveBrowserCredential(
                tenant_id=TENANT_ID
            )

        token = credential.get_token(
            "https://api.fabric.microsoft.com/.default"
        )

        # Types expected by the Fabric notebook
        integer_parameters = {
            "HORIZON_MP_CRITIQUE_JOURS",
            "HORIZON_H_DAYS",
            "ADVANCE_RETREAT_DAYS",
            "MAX_GAP_BETWEEN_OPS_HOURS"
        }

        parameter_names = [
            "CAPACITE_MODE",
            "MODE_PLANIF",
            "SIM_DESC",
            "SIM_USER",
            "SIM_DIVISIONS",
            "SIM_LAUNCH_DATE",
            "SIM_ARRETS_JSON",
            "VERIF_STOCK",
            "VERIF_ECART_OPS",
            "VERIF_FENETRE",
            "HORIZON_MP_CRITIQUE_JOURS",
            "HORIZON_H_DAYS",
            "ADVANCE_RETREAT_DAYS",
            "MAX_GAP_BETWEEN_OPS_HOURS",
            "HORIZON_DEB",
            "HORIZON_FIN",
            "LOG_LEVEL",
            "LOG_FILTER"
        ]

        parameters = []

        for name in parameter_names:
            if name not in data:
                return jsonify({
                    "success": False,
                    "message": f"Paramètre manquant : {name}"
                }), 400

            value = data[name]

            if name in integer_parameters:
                value = int(value)
                param_type = "Number"
            else:
                value = str(value)
                param_type = "Text"

            parameters.append({
                "name": name,
                "value": value,
                "type": param_type
            })

        url = (
            f"{FABRIC_API}/workspaces/{WORKSPACE_ID}"
            f"/notebooks/{NOTEBOOK_ID}"
            "/jobs/execute/instances?beta=false"
        )

        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token.token}",
                "Content-Type": "application/json"
            },
            json={"parameters": parameters},
            timeout=60
        )

        if response.status_code == 202:
            location = response.headers.get("Location", "")
            job_id = location.rstrip("/").split("/")[-1]

            job_locations[job_id] = location

            return jsonify({
                "success": True,
                "message": "Simulation envoyée à Fabric.",
                "job_id": job_id,
                "retry_after": response.headers.get("Retry-After", "60")
            }), 202

        return jsonify({
            "success": False,
            "status": response.status_code,
            "message": response.text
        }), response.status_code

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


@app.route("/api/job-status/<job_id>")
def job_status(job_id):
    try:
        global credential

        location = job_locations.get(job_id)

        if not location:
            return jsonify({
                "success": False,
                "message": "Job introuvable."
            }), 404

        if credential is None:
            credential = InteractiveBrowserCredential(
                tenant_id=TENANT_ID
            )

        token = credential.get_token(
            "https://api.fabric.microsoft.com/.default"
        )

        response = requests.get(
            location,
            headers={
                "Authorization": f"Bearer {token.token}"
            },
            timeout=30
        )

        result = response.json()

        if response.status_code != 200:
            return jsonify({
                "success": False,
                "message": result
            }), response.status_code

        return jsonify({
            "success": True,
            "status": result.get("status"),
            "exit_value": (
                (result.get("properties") or {}).get("exitValue")
                or result.get("exitValue")
            ),
            "result": result
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("FLASK_PORT", 5000)),
        debug=False
    )