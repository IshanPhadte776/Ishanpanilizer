"""
Pipeline debug viewer -- render any stage of the IFC -> CityJSON -> panels
pipeline on its own, so you can see where geometry goes wrong.

    # stage 0: raw IFC straight from ifcopenshell (ground truth)
    python scripts/view_model.py --ifc "model.ifc"

    # stage 2: the converted CityJSON, exactly as building_parser.py hands it to the panelizer
    python scripts/view_model.py --cityjson input/<name>/model.json

    # both overlaid: raw IFC as grey wireframe, CityJSON solid on top
    python scripts/view_model.py --ifc "model.ifc" --cityjson input/<name>/model.json

Filters:
    --classes IfcWall,IfcRoof     which IFC classes to draw (default: the envelope set; "all" for everything)
    --exterior-only               apply the same exterior classification the converter uses
    --semantic WallSurface        only draw these CityJSON semantic types
    --list                        print a breakdown and exit without opening a window
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import open3d as o3d

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ifcopenshell  # noqa: E402
import trimesh  # noqa: E402
import ifc_to_cityjson as conv  # noqa: E402  (same scripts/ directory)

from src.building_parser import SEMANTIC_COLORS, build_lod3_building_dictionaries, semantic_key  # noqa: E402


ENVELOPE_CLASSES = ["IfcWall", "IfcRoof", "IfcSlab", "IfcWindow", "IfcDoor"]

IFC_CLASS_COLORS = {
    "IfcWall": [0.78, 0.66, 0.51],
    "IfcRoof": [0.75, 0.22, 0.17],
    "IfcSlab": [0.50, 0.55, 0.55],
    "IfcWindow": [0.36, 0.68, 0.89],
    "IfcDoor": [0.90, 0.49, 0.13],
}
DEFAULT_COLOR = [0.74, 0.76, 0.78]


def load_ifc_meshes(ifc_path, classes, exterior_only):
    """Raw IFC geometry, one item per element, colored by IFC class."""
    ifc_file = ifcopenshell.open(ifc_path)
    settings = conv.make_settings()
    warnings = []

    items = []
    counts = defaultdict(int)
    skipped = 0

    for ifc_class in classes:
        for element in ifc_file.by_type(ifc_class):
            # only count the element under the class we asked for (by_type is inclusive of subtypes)
            if not element.is_a(ifc_class):
                continue

            if exterior_only:
                if element.is_a("IfcWall"):
                    is_ext, _ = conv.classify_wall(element, warnings)
                    if not is_ext:
                        continue
                elif element.is_a("IfcSlab"):
                    is_ext, _ = conv.classify_slab(element, warnings)
                    if not is_ext:
                        continue

            tri = conv.create_mesh(element, settings)
            if tri is None:
                skipped += 1
                continue

            items.append({"tri": tri, "color": IFC_CLASS_COLORS.get(element.is_a(), DEFAULT_COLOR)})
            counts[element.is_a()] += 1

    return items, counts, skipped


def load_cityjson_meshes(cityjson_path, semantic_filter):
    """CityJSON surfaces as building_parser.py builds them -- i.e. exactly what the panelizer consumes."""
    building = build_lod3_building_dictionaries(Path(cityjson_path))

    items = []
    counts = defaultdict(int)
    wanted = None
    if semantic_filter:
        wanted = {semantic_key(s) for s in semantic_filter}

    for sem_type in SEMANTIC_COLORS:
        key = semantic_key(sem_type)
        for surface in building["surfaces"].get(key, []):
            if wanted is not None and key not in wanted:
                continue
            mesh = surface["mesh"]
            tri = trimesh.Trimesh(
                vertices=np.asarray(mesh.vertices, dtype=float),
                faces=np.asarray(mesh.triangles, dtype=np.int64),
                process=False,
            )
            items.append({"tri": tri, "color": SEMANTIC_COLORS.get(surface["semantic_type"], DEFAULT_COLOR)})
            counts[surface["semantic_type"]] += 1

    return items, counts, building


def as_o3d(items):
    meshes = []
    for item in items:
        tri = item["tri"]
        if len(tri.faces) == 0:
            continue
        mesh = o3d.geometry.TriangleMesh()
        mesh.vertices = o3d.utility.Vector3dVector(np.asarray(tri.vertices, dtype=float))
        mesh.triangles = o3d.utility.Vector3iVector(np.asarray(tri.faces, dtype=np.int32))
        mesh.compute_vertex_normals()
        mesh.paint_uniform_color(item["color"])
        meshes.append(mesh)
    return meshes


def export_glb(items, path):
    """Write the same geometry out as a GLB so it can be opened in the browser.

    Each mesh gets an explicit non-metallic PBR material. Without one, glTF's default
    material applies (metallic 1.0), and a fully metallic surface with no environment map
    renders solid black in every compliant viewer.
    """
    scene = trimesh.Scene()
    for index, item in enumerate(items):
        tri = item["tri"].copy()
        if len(tri.faces) == 0:
            continue
        rgba = [*(float(c) for c in item["color"]), 1.0]
        tri.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                baseColorFactor=rgba,
                metallicFactor=0.0,
                roughnessFactor=0.85,
                doubleSided=True,
            )
        )
        scene.add_geometry(tri, node_name=f"e{index}")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trimesh.exchange.gltf.export_glb(scene))
    print(f"Wrote {path} ({len(scene.geometry)} meshes)")


def to_wireframe(meshes, color):
    lines = []
    for mesh in meshes:
        ls = o3d.geometry.LineSet.create_from_triangle_mesh(mesh)
        ls.paint_uniform_color(color)
        lines.append(ls)
    return lines


def describe(title, counts, extra=""):
    total = sum(counts.values())
    print(f"\n{title}: {total} element(s){extra}")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {count}")


def show(geometries, title, screenshot=None):
    if not geometries:
        print("Nothing to draw.")
        return

    if screenshot:
        vis = o3d.visualization.Visualizer()
        vis.create_window(window_name=title, width=1600, height=1000, visible=True)
        for g in geometries:
            vis.add_geometry(g)
        vis.reset_view_point(True)

        ctl = vis.get_view_control()
        ctl.set_front([0.45, -0.75, 0.48])
        ctl.set_up([0.0, 0.0, 1.0])
        ctl.set_zoom(0.72)

        opt = vis.get_render_option()
        opt.background_color = np.asarray([1.0, 1.0, 1.0])
        opt.mesh_show_back_face = True

        for _ in range(5):
            vis.poll_events()
            vis.update_renderer()
        vis.capture_screen_image(str(screenshot), do_render=True)
        vis.destroy_window()
        print(f"Saved screenshot to {screenshot}")
        return

    o3d.visualization.draw_geometries(geometries, window_name=title, width=1600, height=1000)


def main():
    parser = argparse.ArgumentParser(description="Render a single stage of the IFC -> CityJSON pipeline.")
    parser.add_argument("--ifc", help="Render raw IFC geometry from this file")
    parser.add_argument("--cityjson", help="Render the converted CityJSON from this file")
    parser.add_argument("--classes", default=",".join(ENVELOPE_CLASSES),
                        help='IFC classes to draw, comma separated (default: envelope set; "all" for every product)')
    parser.add_argument("--exterior-only", action="store_true",
                        help="Apply the converter's own exterior classification to walls/slabs")
    parser.add_argument("--semantic", help="Only draw these CityJSON semantic types, comma separated")
    parser.add_argument("--wireframe", action="store_true", help="Draw the IFC side as wireframe even when alone")
    parser.add_argument("--list", action="store_true", help="Print a breakdown and exit without rendering")
    parser.add_argument("--screenshot", help="Render once to this PNG instead of opening an interactive window")
    parser.add_argument("--export-glb", help="Write the geometry to this .glb (for the browser) instead of rendering")
    args = parser.parse_args()

    if not args.ifc and not args.cityjson:
        parser.error("pass --ifc, --cityjson, or both")

    geometries = []
    titles = []

    ifc_items = []
    if args.ifc:
        if args.classes.strip().lower() == "all":
            classes = ["IfcProduct"]
        else:
            classes = [c.strip() for c in args.classes.split(",") if c.strip()]
        ifc_items, counts, skipped = load_ifc_meshes(args.ifc, classes, args.exterior_only)
        extra = f", {skipped} with no geometry" if skipped else ""
        if args.exterior_only:
            extra += " [exterior-only]"
        describe(f"Raw IFC  {Path(args.ifc).name}", counts, extra)
        titles.append("raw IFC")

    cj_items = []
    if args.cityjson:
        semantic_filter = [s.strip() for s in args.semantic.split(",")] if args.semantic else None
        cj_items, counts, building = load_cityjson_meshes(args.cityjson, semantic_filter)
        describe(f"CityJSON {Path(args.cityjson).name}", counts, f" [lod {building['lod']}]")
        titles.append("CityJSON")

    if args.list:
        return

    if args.export_glb:
        export_glb(ifc_items + cj_items, args.export_glb)
        return

    ifc_meshes = as_o3d(ifc_items)
    cj_meshes = as_o3d(cj_items)

    # overlay mode: IFC drops back to a grey wireframe so the converted surfaces read on top
    if ifc_meshes and cj_meshes:
        geometries.extend(to_wireframe(ifc_meshes, [0.45, 0.48, 0.52]))
        geometries.extend(cj_meshes)
        print("\nOverlay: grey wireframe = raw IFC, solid = converted CityJSON")
    elif ifc_meshes:
        geometries.extend(to_wireframe(ifc_meshes, [0.2, 0.2, 0.2]) if args.wireframe else ifc_meshes)
    else:
        geometries.extend(cj_meshes)

    show(geometries, " + ".join(titles), args.screenshot)


if __name__ == "__main__":
    main()
