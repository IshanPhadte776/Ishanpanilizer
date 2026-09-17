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
import ifcopenshell.util.placement as ifc_placement  # noqa: E402
import trimesh  # noqa: E402
import ifc_to_cityjson as conv  # noqa: E402  (same scripts/ directory)

from src.building_parser import SEMANTIC_COLORS, build_lod3_building_dictionaries, semantic_key  # noqa: E402


ENVELOPE_CLASSES = ["IfcWall", "IfcRoof", "IfcSlab", "IfcWindow", "IfcDoor", "IfcPlate", "IfcMember"]

IFC_CLASS_COLORS = {
    "IfcWall": [0.78, 0.66, 0.51],
    "IfcRoof": [0.75, 0.22, 0.17],
    "IfcSlab": [0.50, 0.55, 0.55],
    "IfcWindow": [0.36, 0.68, 0.89],
    "IfcDoor": [0.90, 0.49, 0.13],
    # Curtain walls (IfcCurtainWall) never carry their own geometry -- they're pure
    # IsDecomposedBy containers over these two, which is why they're listed here instead:
    "IfcPlate": [0.40, 0.70, 0.86],  # the glazing infill
    "IfcMember": [0.45, 0.47, 0.50],  # mullions/transoms/framing
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

            items.append({
                "tri": tri,
                "color": IFC_CLASS_COLORS.get(element.is_a(), DEFAULT_COLOR),
                "group": element.is_a(),  # merge key for export_glb
            })
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
            items.append({
                "tri": tri,
                "color": SEMANTIC_COLORS.get(surface["semantic_type"], DEFAULT_COLOR),
                "group": surface["semantic_type"],  # merge key for export_glb
            })
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
    """Write the geometry out as a GLB so it can be opened in the browser, MERGED into one
    mesh per class/semantic type rather than one mesh per element.

    Every mesh in a glTF scene is its own draw call, and a BIM model is thousands of tiny
    elements: Glengarry is 18,621 elements but only 358k triangles -- 19 triangles per draw
    call, mostly individual curtain-wall mullions. WebGL sustains roughly 1-3k draw calls a
    frame, so that model stutters despite having *fewer* triangles than Frontenac (378k),
    which renders fine at 5,369 elements. The bottleneck is draw-call count, not geometry
    volume, so the fix is merging rather than decimating: it changes nothing about what is
    drawn, and everything within a group already shares one material anyway.

    Grouping is by (label, colour). Colour is the part that actually has to be uniform --
    one material per merged mesh -- while the label just keeps the glTF node names readable
    and keeps IFC classes from being welded to CityJSON semantics in overlay mode.

    Each group gets an explicit non-metallic PBR material. Without one, glTF's default
    material applies (metallic 1.0), and a fully metallic surface with no environment map
    renders solid black in every compliant viewer.
    """
    groups = defaultdict(list)
    for item in items:
        tri = item["tri"]
        if len(tri.faces) == 0:
            continue
        groups[(item.get("group", "geometry"), tuple(float(c) for c in item["color"]))].append(tri)

    scene = trimesh.Scene()
    total_faces = 0
    for (label, color), meshes in sorted(groups.items()):
        # concatenate before assigning the material: merging meshes that already carry
        # TextureVisuals makes trimesh try to reconcile materials per-mesh, which is both
        # slower and pointless when the whole group is one flat colour
        merged = trimesh.util.concatenate(meshes) if len(meshes) > 1 else meshes[0].copy()
        merged.visual = trimesh.visual.TextureVisuals(
            material=trimesh.visual.material.PBRMaterial(
                baseColorFactor=[*color, 1.0],
                metallicFactor=0.0,
                roughnessFactor=0.85,
                doubleSided=True,
            )
        )
        scene.add_geometry(merged, node_name=label)
        total_faces += len(merged.faces)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trimesh.exchange.gltf.export_glb(scene))

    element_count = sum(len(meshes) for meshes in groups.values())
    print(f"Wrote {path}")
    print(f"  {element_count} elements -> {len(scene.geometry)} draw call(s), {total_faces:,} triangles")
    for (label, _color), meshes in sorted(groups.items()):
        print(f"    {label}: {len(meshes)} elements merged")


def compute_grade_z(ifc_path):
    """Z, in meters, in the raw pre-recentre project frame, of the lowest storey NOT
    classified as below-grade -- i.e. where real ground level actually is.

    Returns None when there's nothing to correct for (a single-storey file, or nothing
    detected as a basement by detect_basement_storeys), in which case
    recentre_far_from_origin's fallback -- put the model's lowest point at z=0 -- is
    already the right call.
    """
    ifc_file = ifcopenshell.open(ifc_path)
    storeys = [s for s in ifc_file.by_type("IfcBuildingStorey")
               if "survey point" not in (s.Name or "").lower()]
    if len(storeys) < 2:
        return None

    wall_storey, curtain_wall_storey, wall_count, glazing_count = conv.build_storey_glazing_index(ifc_file)
    basement_ids = conv.detect_basement_storeys(ifc_file, wall_count, glazing_count, [])
    if not basement_ids:
        return None

    storeys.sort(key=lambda s: s.Elevation if s.Elevation is not None else 0.0)
    grade_storey = next((s for s in storeys if s.id() not in basement_ids), None)
    if grade_storey is None:
        return None

    # get_local_placement returns the raw file units (mm for these projects); ifcopenshell.geom
    # meshes are always meters, so the two frames only line up once this is scaled the same way.
    unit_scale = conv.ifc_unit.calculate_unit_scale(ifc_file)
    matrix = ifc_placement.get_local_placement(grade_storey.ObjectPlacement)
    return float(matrix[2, 3]) * unit_scale


def recentre_far_from_origin(items, grade_z=None):
    """Shift geometry (in float64, in place) so it sits near the origin before it ever
    reaches a float32 buffer, and so that z=0 means the grid represents actual ground
    level rather than "whatever the model's lowest point happens to be."

    Revit/IFC exports routinely carry real survey coordinates (easting/northing in the
    hundreds of thousands to millions of meters) even though the building itself only
    spans tens of meters. glTF/GLB stores vertex positions as float32, which only has
    ~7 significant decimal digits -- at a magnitude of ~5,000,000 that leaves a rounding
    step of roughly half a meter, so vertices snap to a coarse grid and every frame's
    view-matrix multiply lands on a slightly different grid point: the model visibly
    jitters/"shakes" even with a perfectly static camera. Recentring afterwards at the
    Object3D/transform level (as the browser viewer used to) does not fix this -- the
    precision is already lost by the time the vertex leaves this script. The fix has to
    happen here, on the raw float64 coordinates, before export.

    `grade_z` (from compute_grade_z) lets basement storeys land below z=0/the grid
    instead of being the thing that defines z=0 -- without it, a basement's floor slab is
    the model's lowest point, so the old "sit the lowest point on the grid" behaviour
    would put the basement ON the grid rather than under it.
    """
    verts = [it["tri"].vertices for it in items if len(it["tri"].vertices)]
    if not verts:
        return
    bbox_min = np.min([v.min(axis=0) for v in verts], axis=0)
    bbox_max = np.max([v.max(axis=0) for v in verts], axis=0)
    z_ref = grade_z if grade_z is not None else bbox_min[2]
    offset = np.array([(bbox_min[0] + bbox_max[0]) / 2.0, (bbox_min[1] + bbox_max[1]) / 2.0, z_ref])

    reason = "grade level (basement will sit below z=0)" if grade_z is not None else "the model's lowest point"
    print(f"Recentring: shifting by {-offset} so {reason} is at the origin "
          f"(source coordinates were ~{np.linalg.norm(offset):,.0f} m from it).")
    for it in items:
        if len(it["tri"].vertices):
            it["tri"].vertices = it["tri"].vertices - offset


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

    grade_z = compute_grade_z(args.ifc) if args.ifc else None
    if grade_z is not None:
        print(f"Detected a basement -- grade level is at z={grade_z:.3f} m in the source file's frame.")
    recentre_far_from_origin(ifc_items + cj_items, grade_z=grade_z)

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
