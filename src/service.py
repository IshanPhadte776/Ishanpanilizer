import time
from pathlib import Path

from .building_parser import build_lod3_building_dictionaries, load_json_file
from .panelizer import load_panelizer_config, panelize_buildings


def select_buildings(buildings: list[dict], selected_indices: list[int] | None) -> list[dict]:
    if selected_indices is None:
        return buildings
    return [buildings[index] for index in selected_indices]


def load_buildings(input_json: Path | str, selected_indices: list[int] | None = None) -> list[dict]:
    input_json = Path(input_json)
    cityjson = load_json_file(input_json)
    buildings = build_lod3_building_dictionaries({input_json: cityjson})
    return select_buildings(buildings, selected_indices)


def run_panelization(config: dict, output_json_path: Path | str | None = None) -> tuple[list[dict], dict]:
    """Load the CityJSON and panelize it, timing the two phases separately.

    They're worth separating because they scale on different things and are fixed in
    different places: model load is CityJSON parsing plus triangulating every surface (it
    depends on model size and repeats identically for every run against the same file, so
    it's the part worth caching), while panelize depends on panel size and layout settings
    and genuinely has to rerun each time they change.

    The timings ride along on the returned dict rather than being logged, so the API can
    hand them to the UI. Note the panels JSON written by panelize_buildings is saved before
    this point and so doesn't carry them -- they're a runtime measurement, not model data.
    """
    load_started = time.perf_counter()
    buildings = load_buildings(config["input_json"], config.get("selected_building_indices"))
    model_ms = (time.perf_counter() - load_started) * 1000.0

    panelize_started = time.perf_counter()
    panelization = panelize_buildings(buildings, config=config, output_json_path=output_json_path)
    panelize_ms = (time.perf_counter() - panelize_started) * 1000.0

    panelization["timings"] = {
        "model_ms": round(model_ms, 1),
        "panelize_ms": round(panelize_ms, 1),
        "total_ms": round(model_ms + panelize_ms, 1),
    }
    return buildings, panelization


def load_config_and_run(config_path: Path | str) -> tuple[list[dict], dict]:
    config = load_panelizer_config(config_path)
    return run_panelization(config, output_json_path=config["output_json"])


OBJECTIVES = {
    "cost": lambda summary: summary["cost_total"],
    "types": lambda summary: summary["total_unique_types"],
    "panels": lambda summary: summary["total_panels"],
}


def search_panelization(config: dict, seeds, objective: str = "cost") -> list[dict]:
    """Panelize once per seed and return the results ranked, best first.

    A re-rolled layout costs more than the plain aligned grid (sliding the grid off a wall's
    corner leaves a partial panel at BOTH edges instead of one, and every partial is another
    unique type), so searching is how you get variation without paying the worst of it.
    Pass `None` among the seeds to include the aligned grid as a baseline to rank against.

    The CityJSON is loaded and parsed once, not per seed -- only the layout on top of the
    geometry changes.
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {sorted(OBJECTIVES)}")
    score = OBJECTIVES[objective]

    buildings = load_buildings(config["input_json"], config.get("selected_building_indices"))

    results = []
    for seed in seeds:
        trial = dict(config)
        trial["seed"] = seed
        panelization = panelize_buildings(buildings, config=trial)
        results.append({"seed": seed, "summary": panelization["summary"]})

    results.sort(key=lambda result: score(result["summary"]))
    return results


def building_to_viewer_payload(buildings: list[dict]) -> dict:
    parts = []
    categories = ["roof", "wall", "reveal", "balcony", "other"]
    for building in buildings:
        surfaces = []
        for category in categories:
            for surface in building.get("surfaces", {}).get(category, []):
                surfaces.append({
                    "category": category,
                    "semantic_type": surface["semantic_type"],
                    "semantic_key": surface["semantic_key"],
                    "rings": surface["rings"],
                    "is_basement": surface.get("is_basement", False),
                })

        parts.append({
            "building_id": building["id"],
            "parent_id": str(building["id"]).split("-")[0],
            "surfaces": surfaces,
        })

    return {"parts": parts}


def panelization_to_viewer_payload(panelization: dict) -> dict:
    return {
        "building_id": panelization["building_id"],
        "summary": panelization["summary"],
        "parts": panelization["parts"],
        "timings": panelization.get("timings", {}),
    }
