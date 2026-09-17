import json
import re
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .api_models import BuildingRequest, PanelizeRequest
from .panelizer import load_panelizer_config, shutdown_worker_pool
from .service import (
    building_to_viewer_payload,
    load_buildings,
    panelization_to_viewer_payload,
    run_panelization,
)

UPLOAD_DIR = Path("input/uploads")
REPO_ROOT = Path(__file__).resolve().parent.parent
# ifc_to_cityjson.py's own occlusion ray-casting is the slow part -- Glengarry (77MB, 6,077
# walls) still finished in a few minutes, but there's no hard upper bound for an arbitrary
# upload, so this is a backstop, not a tuned expectation.
IFC_PIPELINE_TIMEOUT_S = 1800


app = FastAPI(title="Panilizer API", version="0.1.0")


@app.on_event("shutdown")
def _shutdown():
    # Release the parallel-panelize worker pool (see panelizer.get_worker_pool) so its
    # processes don't linger after uvicorn exits.
    shutdown_worker_pool()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def config():
    config_data = load_panelizer_config("config/panelizer_config.json")
    return {
        key: value
        for key, value in config_data.items()
        if key not in {"tolerance", "precision"}
    }


@app.post("/upload")
def upload_model(file: UploadFile = File(...)):
    """Accept either a CityJSON model.json, or a raw .ifc (run through
    scripts/ifc_to_cityjson.py + sanitize_cityjson.py first). Saves the result under
    input/uploads/ and hands back the path to feed straight into /panelize.

    A plain (non-async) def, deliberately: FastAPI runs sync route handlers in a worker
    thread automatically, which matters here because the .ifc path can run the extraction
    pipeline for minutes -- an async def would block the whole event loop (every other
    request, including /health) for that entire time, since nothing in it would ever
    yield control back with an await.
    """
    filename = file.filename or "model.json"
    contents = file.file.read()  # sync read; file.file is a SpooledTemporaryFile
    suffix = Path(filename).suffix.lower()

    if suffix == ".ifc":
        return _handle_ifc_upload(filename, contents)
    return _handle_cityjson_upload(filename, contents)


def _handle_cityjson_upload(filename: str, contents: bytes) -> dict:
    try:
        data = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "CityObjects" not in data:
        raise HTTPException(
            status_code=400,
            detail="Doesn't look like a CityJSON file (no 'CityObjects' key) or a .ifc file. "
                   "Upload a CityJSON model.json or a raw .ifc.",
        )

    # strip any path components the browser might send, keep only the filename
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".json"):
        safe_name += ".json"

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(contents)

    return {"input_json": dest.as_posix()}


def _slugify(name: str) -> str:
    # matches scripts/run_ifc_demo.ps1's own slug rule, so an upload lands in the same
    # shape of directory a manual run through that script would produce
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return (slug[:48].strip("_") or "model") if len(slug) > 48 else (slug or "model")


def _handle_ifc_upload(filename: str, contents: bytes) -> dict:
    slug = _slugify(Path(filename).stem)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    ifc_path = UPLOAD_DIR / f"{slug}.ifc"
    ifc_path.write_bytes(contents)

    out_dir = UPLOAD_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    model_raw = out_dir / "model_raw.json"
    model_json = out_dir / "model.json"

    python_exe = sys.executable  # the same interpreter/venv running this server

    extract = subprocess.run(
        [python_exe, str(REPO_ROOT / "scripts" / "ifc_to_cityjson.py"),
         "--ifc", str(ifc_path), "--out-dir", str(out_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=IFC_PIPELINE_TIMEOUT_S,
    )
    if extract.returncode != 0:
        raise HTTPException(
            status_code=422,
            detail=f"IFC -> CityJSON extraction failed (exit {extract.returncode}):\n"
                   f"{_tail(extract.stderr)}",
        )
    if not model_raw.exists():
        raise HTTPException(
            status_code=422,
            detail=f"ifc_to_cityjson.py exited 0 but didn't write {model_raw.name}. Output:\n{_tail(extract.stdout)}",
        )

    sanitize = subprocess.run(
        [python_exe, str(REPO_ROOT / "scripts" / "sanitize_cityjson.py"), str(model_raw), str(model_json)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    if sanitize.returncode != 0:
        raise HTTPException(
            status_code=422,
            detail=f"CityJSON sanitize failed (exit {sanitize.returncode}):\n{_tail(sanitize.stderr)}",
        )

    # ifc_to_cityjson.py's classification heuristics (Workset/type-name conventions) were
    # tuned against this project's own IFC exports (see the colonel-by/frontenac data-quirk
    # notes). A file using a naming convention it doesn't recognize extracts with EXIT 0 and
    # no error -- classify_wall just excludes every wall as "no signal" -- so this checks the
    # actual output for real WallSurface entries rather than trusting a clean exit code.
    # (Warning *count* isn't a useful proxy: Frontenac/Glengarry both produce thousands of
    # warnings on otherwise-correct extractions.)
    wall_count = _count_wall_surfaces(model_json)
    response = {"input_json": model_json.as_posix(), "wall_count": wall_count}
    if wall_count == 0:
        response["warning"] = (
            "Extraction finished with no errors, but found 0 exterior WallSurface entries. "
            "This IFC likely uses a Workset/type-name convention classify_wall doesn't "
            "recognize yet (scripts/ifc_to_cityjson.py) -- panelizing this will show 0 walls."
        )
    return response


def _count_wall_surfaces(model_json_path: Path) -> int:
    try:
        data = json.loads(model_json_path.read_text(encoding="utf-8"))
        geometry = data["CityObjects"]["Building_1"]["geometry"][0]
        surfaces = geometry["semantics"]["surfaces"]
        values = geometry["semantics"]["values"]
        return sum(1 for value in values if surfaces[value]["type"] == "WallSurface")
    except Exception:
        return -1  # couldn't tell -- don't claim zero when we're not sure


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


@app.post("/buildings")
def buildings(request: BuildingRequest):
    selected = load_buildings(request.input_json, request.selected_building_indices)
    return building_to_viewer_payload(selected)


@app.post("/panelize")
def panelize(request: PanelizeRequest):
    config_data = _request_to_config(request)
    buildings_data, panelization = run_panelization(config_data)
    response = {"panelization": panelization_to_viewer_payload(panelization)}
    if request.include_building:
        response["building"] = building_to_viewer_payload(buildings_data)
    return response


@app.post("/panelize/export")
def panelize_export(request: PanelizeRequest):
    config_data = _request_to_config(request)
    output_json = request.output_json or config_data["output_json"]
    buildings_data, panelization = run_panelization(config_data, output_json_path=output_json)
    return {
        "output_json": str(Path(output_json)),
        "panelization": panelization_to_viewer_payload(panelization),
        "building": building_to_viewer_payload(buildings_data) if request.include_building else None,
    }


def _request_to_config(request: PanelizeRequest) -> dict:
    config_data = load_panelizer_config("config/panelizer_config.json")
    config_data.update({
        "input_json": request.input_json,
        "selected_building_indices": request.selected_building_indices,
        "panel_width": request.settings.panel_width,
        "panel_height": request.settings.panel_height,
        "cost_per_unique_panel_type": request.settings.cost_per_unique_panel_type,
        "cost_per_panel_element": request.settings.cost_per_panel_element,
        "seed": request.settings.seed,
        "origin_jitter": request.settings.origin_jitter,
        "stagger": request.settings.stagger,
        "include_basement": request.settings.include_basement,
        # The API never sends panel_meshes to the client (panelization_to_viewer_payload
        # doesn't include them) or needs the local desktop viewer, so meshes are always
        # skipped here -- which is also what makes the parallel worker pool usable at all
        # (an open3d TriangleMesh can't be pickled back across a process boundary). This
        # server process stays alive across requests, which is what makes a persistent
        # pool worth it in the first place (see get_worker_pool / DEFAULT_CONFIG["parallel"]).
        "visualize": False,
        "parallel": True,
    })
    if request.output_json is not None:
        config_data["output_json"] = request.output_json
    return config_data
