"""
Stage 3: sanitize a raw CityJSON file -- dedupe/drop orphan vertices via the
cjio CLI, then verify the invariants the downstream panelizer actually relies
on (src/building_parser.py).

cjio's own `validate` command needs the optional `cjvalpy` schema validator,
which has no wheel for this platform; when it is unavailable we skip that step
and fall back to the structural checks below, which cover what actually
determines whether the panelizer can consume the file.

Usage:
    python scripts/sanitize_cityjson.py input/outputColonelBy/model_raw.json input/outputColonelBy/model.json
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path


def find_cjio() -> str | None:
    # prefer the cjio installed alongside the running interpreter (venv Scripts/bin),
    # since the venv is usually not "activated" when this script is invoked by path
    local = Path(sys.executable).parent / ("cjio.exe" if sys.platform == "win32" else "cjio")
    if local.exists():
        return str(local)
    return shutil.which("cjio")


def has_cjvalpy() -> bool:
    try:
        import cjvalpy  # noqa: F401
        return True
    except ImportError:
        return False


def run_cjio(src: Path, dst: Path) -> bool:
    cjio = find_cjio()
    if cjio is None:
        print("cjio CLI not found, falling back to manual sanitize.")
        return False

    cmd = [cjio, str(src), "vertices_clean"]
    if has_cjvalpy():
        cmd.append("validate")
    else:
        print("cjvalpy not installed -- skipping cjio's schema validation, using structural checks instead.")
    cmd += ["save", str(dst)]

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0 or not dst.exists():
        print(f"cjio exited with code {result.returncode}, falling back to manual sanitize.")
        return False
    return True


def manual_sanitize(src: Path, dst: Path, precision: int = 3):
    """Dedupe vertices and drop degenerate faces without external tools."""
    with open(src, "r", encoding="utf-8") as fh:
        cj = json.load(fh)

    old_vertices = cj["vertices"]
    transform = cj.get("transform", {"scale": [1, 1, 1], "translate": [0, 0, 0]})
    scale = transform["scale"]
    translate = transform["translate"]

    def world(v):
        return tuple(round(v[i] * scale[i] + translate[i], precision) for i in range(3))

    new_index = {}
    new_vertices = []
    remap = {}
    for old_i, v in enumerate(old_vertices):
        key = world(v)
        if key not in new_index:
            new_index[key] = len(new_vertices)
            new_vertices.append(list(key))
        remap[old_i] = new_index[key]

    dropped_faces = 0
    for obj in cj.get("CityObjects", {}).values():
        for geometry in obj.get("geometry", []):
            new_boundaries = []
            new_values = []
            values = geometry.get("semantics", {}).get("values", [])
            for surface_i, surface in enumerate(geometry.get("boundaries", [])):
                new_rings = []
                for ring in surface:
                    remapped = [remap[i] for i in ring]
                    deduped = [v for i, v in enumerate(remapped) if i == 0 or v != remapped[i - 1]]
                    if len(deduped) > 1 and deduped[0] == deduped[-1]:
                        deduped.pop()
                    if len(deduped) >= 3:
                        new_rings.append(deduped)
                if new_rings:
                    new_boundaries.append(new_rings)
                    if surface_i < len(values):
                        new_values.append(values[surface_i])
                else:
                    dropped_faces += 1
            geometry["boundaries"] = new_boundaries
            if "semantics" in geometry:
                geometry["semantics"]["values"] = new_values

    cj["vertices"] = new_vertices
    cj["transform"] = {"scale": [1.0, 1.0, 1.0], "translate": [0.0, 0.0, 0.0]}

    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(cj, fh)

    print(f"Manual sanitize: {len(old_vertices)} -> {len(new_vertices)} vertices, dropped {dropped_faces} degenerate faces.")


def structural_check(path: Path) -> list[str]:
    """Verify the invariants src/building_parser.py + src/panelizer.py depend on."""
    errors = []
    with open(path, "r", encoding="utf-8") as fh:
        cj = json.load(fh)

    for key in ("type", "version", "CityObjects", "vertices", "transform"):
        if key not in cj:
            errors.append(f"missing top-level key: {key}")
    if errors:
        return errors

    n_verts = len(cj["vertices"])
    for v in cj["vertices"]:
        if len(v) != 3 or any(not math.isfinite(c) for c in v):
            errors.append("vertices array contains a malformed or non-finite coordinate")
            break

    lod3_surfaces = 0
    buildings = 0
    for obj_id, obj in cj["CityObjects"].items():
        if "building" not in str(obj.get("type", "")).lower():
            continue
        buildings += 1
        for geometry in obj.get("geometry", []):
            if str(geometry.get("lod")) != "3":
                continue
            boundaries = geometry.get("boundaries", [])
            values = geometry.get("semantics", {}).get("values", [])
            if geometry.get("type") in ("MultiSurface", "CompositeSurface") and len(values) != len(boundaries):
                errors.append(f"{obj_id}: semantics.values length {len(values)} != boundaries length {len(boundaries)}")
            for surface in boundaries:
                lod3_surfaces += 1
                for ring in surface:
                    if len(set(ring)) < 3:
                        errors.append(f"{obj_id}: ring with fewer than 3 distinct vertices")
                    for idx in ring:
                        if not isinstance(idx, int) or idx < 0 or idx >= n_verts:
                            errors.append(f"{obj_id}: vertex index {idx} out of range (0..{n_verts - 1})")
                            break

    if buildings == 0:
        errors.append("no CityObject whose type contains 'building'")
    if lod3_surfaces == 0:
        errors.append("no LoD3 surfaces found (building_parser.py requires str(lod) == '3')")

    # dedupe repeated messages so one systemic problem doesn't flood the report
    seen = set()
    unique = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    return unique


def main():
    parser = argparse.ArgumentParser(description="Sanitize a raw CityJSON file (cjio vertices_clean/save + structural checks).")
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    args = parser.parse_args()

    if not run_cjio(args.src, args.dst):
        manual_sanitize(args.src, args.dst)

    with open(args.dst, "r", encoding="utf-8") as fh:
        cj = json.load(fh)
    print(f"\n{args.dst}: {len(cj.get('CityObjects', {}))} CityObject(s), {len(cj.get('vertices', []))} vertices")

    errors = structural_check(args.dst)
    if errors:
        print(f"\nStructural check FAILED with {len(errors)} problem(s):")
        for e in errors[:20]:
            print(f"  - {e}")
        raise SystemExit(1)
    print("Structural check passed (LoD3 surfaces, semantics alignment, vertex indices all valid).")


if __name__ == "__main__":
    main()
