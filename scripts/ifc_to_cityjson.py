"""
IFC -> LoD3 CityJSON envelope extractor.

Reads an IFC4 file, classifies exterior walls/roofs/slabs/windows/doors
(building envelope) apart from interior clutter, and emits a LoD3 CityJSON
2.0 Building compatible with src/building_parser.py:
  - one "Building" CityObject
  - one geometry entry: lod "3", type "MultiSurface"
  - each wall becomes ONE WallSurface polygon (outer ring + window/door
    cutouts as holes on that same ring) -- not a merged triangle soup --
    because src/panelizer.py panelizes each surface entry independently.

Usage:
    python scripts/ifc_to_cityjson.py --ifc "COLONEL BY CHILD CARE CENTRE_ifc4_2025_full.ifc" --out-dir input/outputColonelBy
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiPoint
from shapely.ops import unary_union

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as ifc_element
import ifcopenshell.util.unit as ifc_unit
import trimesh


WORLD_UP = np.array([0.0, 0.0, 1.0])
WORLD_DOWN = -WORLD_UP


# ---------------------------------------------------------------- classify

def _psets(element):
    try:
        return ifc_element.get_psets(element)
    except Exception:
        return {}


def _type_name(element):
    for rel in getattr(element, "IsTypedBy", []) or []:
        return rel.RelatingType.Name
    return None


def classify_wall(wall, warnings):
    psets = _psets(wall)
    for props in psets.values():
        if "Workset" in props:
            workset = str(props["Workset"]).strip().upper()
            if "EXTERIOR" in workset:
                return True, "workset"
            if "INTERIOR" in workset:
                return False, "workset"

    tname = (_type_name(wall) or "").upper()
    if "-EXT-" in tname:
        return True, "typename"
    if "-INT-" in tname:
        return False, "typename"

    warnings.append(f"wall {wall.GlobalId} ({tname or 'untyped'}): no Workset/type-name signal, excluding")
    return False, "unknown"


def classify_slab(slab, warnings):
    psets = _psets(slab)
    for props in psets.values():
        if "IsExternal" in props and props["IsExternal"] is not None:
            return bool(props["IsExternal"]), "is_external"

    predefined = getattr(slab, "PredefinedType", None)
    if predefined == "ROOF":
        return True, "predefined_type"
    if predefined in ("BASESLAB", "FLOOR"):
        return False, "predefined_type"

    warnings.append(f"slab {slab.GlobalId}: no IsExternal/PredefinedType signal, treating as ground/interior")
    return False, "unknown"


def build_host_openings(ifc_file):
    """host wall id -> [(opening_element, filling_element), ...] for openings that are actually filled by a window/door."""
    opening_to_host = {}
    for rel in ifc_file.by_type("IfcRelVoidsElement"):
        opening_to_host[rel.RelatedOpeningElement.id()] = rel.RelatingBuildingElement

    host_openings = defaultdict(list)
    for rel in ifc_file.by_type("IfcRelFillsElement"):
        opening = rel.RelatingOpeningElement
        filling = rel.RelatedBuildingElement
        if filling is None or not (filling.is_a("IfcWindow") or filling.is_a("IfcDoor")):
            continue
        host = opening_to_host.get(opening.id())
        if host is not None:
            host_openings[host.id()].append((opening, filling))
    return host_openings


# ----------------------------------------------------------------- meshing

def make_settings():
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    return settings


def create_mesh(element, settings):
    # ifcopenshell.geom (with USE_WORLD_COORDS) returns coordinates already
    # normalized to meters regardless of the file's declared length unit --
    # verified empirically against this file's own mm-declared Qto data.
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
    faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) == 0 or len(verts) == 0:
        return None
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.merge_vertices()
    if len(mesh.faces) == 0:
        return None
    return mesh


# ------------------------------------------------------------ face select

def facets_facing_open_air(occluder, facets, eps, max_dist):
    """Keep only facets that see open air, by casting a ray out along each facet normal.

    Plan-footprint and centroid tests both proved brittle here (U-shaped plan, detached
    outbuildings that never close into a filled region). Ray casting asks the question
    directly: an external facade sees nothing in front of it, while a room-facing inner leaf
    hits the far side of its own room. `max_dist` is what keeps a courtyard-facing facade
    external -- the opposite wing is much further away than any room is deep.
    """
    if occluder is None or not facets:
        return facets

    origins = np.array([f["origin"] + f["normal"] * eps for f in facets])
    directions = np.array([f["normal"] for f in facets])
    locations, index_ray, _ = occluder.ray.intersects_location(
        ray_origins=origins, ray_directions=directions, multiple_hits=False
    )

    blocked = set()
    for location, ray in zip(locations, index_ray):
        if np.linalg.norm(location - origins[ray]) <= max_dist:
            blocked.add(int(ray))

    return [f for i, f in enumerate(facets) if i not in blocked]


def _facet_hull(mesh, facet, to_2D):
    pts = mesh.vertices[np.unique(mesh.faces[facet["faces"]].ravel())]
    homogeneous = np.hstack([pts, np.ones((len(pts), 1))])
    return MultiPoint((to_2D @ homogeneous.T).T[:, :2]).convex_hull


def dedupe_layered_facets(mesh, facets, angle_tol_deg):
    """Drop facets sitting directly behind another facet that faces the same way.

    Multi-layer walls (e.g. a 60mm brick leaf over a 220mm backing) export as stacked
    solids, so one facade plane shows up two or three times at different depths and would
    otherwise be counted repeatedly. Only the frontmost is the claddable surface. A parallel
    facet that does NOT overlap in projection is a different part of the wall, so it stays.
    """
    cos_tol = np.cos(np.radians(angle_tol_deg))
    kept = []
    for facet in sorted(facets, key=lambda f: -(f["origin"] @ f["normal"])):
        to_2D = trimesh.geometry.plane_transform([0.0, 0.0, 0.0], facet["normal"])
        hull = _facet_hull(mesh, facet, to_2D)
        if hull.area <= 0:
            continue
        behind_existing = False
        for other in kept:
            if facet["normal"] @ other["normal"] <= cos_tol:
                continue
            if hull.intersection(_facet_hull(mesh, other, to_2D)).area > 0.5 * hull.area:
                behind_existing = True
                break
        if not behind_existing:
            kept.append(facet)
    return kept


def cluster_planar_facets(mesh, angle_tol_deg, dist_tol):
    """Group a mesh's faces into planar patches (same normal direction, same plane offset).

    A single IfcWall in this model is often a multi-segment polyline sweep, so its outer
    side is several planes at different angles rather than one. CityJSON surfaces must be
    planar anyway, so each patch becomes its own surface.
    """
    normals = mesh.face_normals
    centroids = mesh.triangles.mean(axis=1)
    areas = mesh.area_faces
    cos_tol = np.cos(np.radians(angle_tol_deg))

    assigned = np.full(len(normals), -1)
    clusters = []

    for seed in np.argsort(-areas):
        if assigned[seed] != -1:
            continue
        seed_normal = normals[seed]
        seed_origin = centroids[seed]

        free = np.where(assigned == -1)[0]
        same_direction = normals[free] @ seed_normal > cos_tol
        on_plane = np.abs((centroids[free] - seed_origin) @ seed_normal) < dist_tol
        members = free[same_direction & on_plane]
        if len(members) == 0:
            members = np.array([seed])

        assigned[members] = len(clusters)
        weights = areas[members]
        normal = np.average(normals[members], axis=0, weights=weights)
        norm = np.linalg.norm(normal)
        normal = seed_normal if norm < 1e-12 else normal / norm
        clusters.append({
            "faces": members,
            "normal": normal,
            "origin": np.average(centroids[members], axis=0, weights=weights),
            "area": float(weights.sum()),
        })

    return clusters


# ------------------------------------------------------------- boundaries

def extract_boundary_polygon(mesh, face_idxs, normal, origin):
    try:
        outline = mesh.outline(face_ids=face_idxs)
    except Exception:
        return None, None
    to_2D = trimesh.geometry.plane_transform(origin, normal)
    try:
        planar, to_3D = outline.to_2D(to_2D=to_2D, check=False)
    except Exception:
        return None, None
    polys = planar.polygons_full
    if not polys:
        return None, None
    poly = max(polys, key=lambda p: p.area)
    return poly, to_3D


def largest_polygon(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon" and not g.is_empty]
        if not polys:
            return None
        return max(polys, key=lambda p: p.area)
    return None


def clean_polygon(poly, grid_size):
    if poly is None:
        return None
    poly = shapely.set_precision(poly, grid_size=grid_size)
    if not poly.is_valid:
        poly = shapely.make_valid(poly)
    return largest_polygon(poly)


def polygon_to_3d_rings(poly, to_3D):
    def ring_to_3d(coords2d):
        pts = np.array([[x, y, 0.0, 1.0] for x, y in coords2d])
        return (to_3D @ pts.T).T[:, :3].tolist()

    rings = [ring_to_3d(list(poly.exterior.coords)[:-1])]
    for interior in poly.interiors:
        rings.append(ring_to_3d(list(interior.coords)[:-1]))
    return rings


def opening_footprint(filling_element, settings, to_2D_transform):
    mesh = create_mesh(filling_element, settings)
    if mesh is None:
        return None
    pts_h = np.hstack([mesh.vertices, np.ones((len(mesh.vertices), 1))])
    pts2d = (to_2D_transform @ pts_h.T).T[:, :2]
    hull = MultiPoint(pts2d).convex_hull
    if hull.geom_type != "Polygon" or hull.area <= 1e-8:
        return None
    return hull


# ------------------------------------------------------------- assembly

def build_wall_records(wall, settings, occluder, host_openings, angle_tol_deg, plane_tol,
                       min_facet_area, ray_eps, ray_max_dist, grid_size, warnings):
    label = f"wall {wall.GlobalId}"
    mesh = create_mesh(wall, settings)
    if mesh is None:
        warnings.append(f"{label}: no geometry, skipped")
        return []

    clusters = cluster_planar_facets(mesh, angle_tol_deg, plane_tol)

    # keep the roughly-vertical, outward-facing patches: that drops the top/bottom caps,
    # the inner leaf, and the thin end strips that aren't claddable facade
    facets = [c for c in clusters if abs(c["normal"][2]) < 0.7 and c["area"] >= min_facet_area]
    facets = facets_facing_open_air(occluder, facets, ray_eps, ray_max_dist)
    facets = dedupe_layered_facets(mesh, facets, angle_tol_deg)
    if not facets:
        warnings.append(f"{label}: no outward-facing vertical facet, skipped")
        return []

    openings = host_openings.get(wall.id(), [])
    records = []
    claimed = set()

    for facet in facets:
        poly, to_3D = extract_boundary_polygon(mesh, facet["faces"], facet["normal"], facet["origin"])
        poly = clean_polygon(poly, grid_size)
        if poly is None or poly.area <= 1e-6:
            continue

        to_2D = np.linalg.inv(to_3D)

        # an opening belongs to whichever facet it actually overlaps
        cuts = []
        for index, (_opening, filling) in enumerate(openings):
            footprint = opening_footprint(filling, settings, to_2D)
            if footprint is None:
                continue
            overlap = footprint.intersection(poly)
            if overlap.is_empty or overlap.area <= 1e-6:
                continue
            cuts.append(footprint)
            if index not in claimed:
                claimed.add(index)
                sem = "Window" if filling.is_a("IfcWindow") else "Door"
                trimmed = clean_polygon(footprint, grid_size) or footprint
                records.append({"semantic_type": sem, "rings": polygon_to_3d_rings(trimmed, to_3D)})

        wall_poly = poly
        if cuts:
            cut = clean_polygon(poly.difference(unary_union(cuts)), grid_size)
            if cut is not None and cut.area > 1e-6:
                wall_poly = cut
            else:
                warnings.append(f"{label}: opening subtraction emptied a facet, keeping it uncut")

        records.append({"semantic_type": "WallSurface", "rings": polygon_to_3d_rings(wall_poly, to_3D)})

    if len(facets) > 1:
        warnings.append(f"{label}: multi-segment wall split into {len(facets)} facets")

    return records


def build_flat_record(element, settings, semantic_type, preferred_dir, angle_tol_deg, plane_tol,
                      min_facet_area, grid_size, warnings):
    label = f"{element.is_a()} {element.GlobalId}"
    mesh = create_mesh(element, settings)
    if mesh is None:
        # IfcRoof is often just an aggregate container whose real surfaces are child
        # IfcSlabs; those get picked up separately, so this isn't a loss
        if not (element.is_a("IfcRoof") and (getattr(element, "IsDecomposedBy", None) or [])):
            warnings.append(f"{label}: no geometry, skipped")
        return []

    # one surface per planar facet, so a pitched roof keeps both slopes instead of
    # being averaged into a single flat plane
    facets = [
        c for c in cluster_planar_facets(mesh, angle_tol_deg, plane_tol)
        if c["normal"] @ preferred_dir > 0.5 and c["area"] >= min_facet_area
    ]
    if not facets:
        warnings.append(f"{label}: no face aligned with expected direction, skipped")
        return []

    records = []
    for facet in facets:
        poly, to_3D = extract_boundary_polygon(mesh, facet["faces"], facet["normal"], facet["origin"])
        poly = clean_polygon(poly, grid_size)
        if poly is None or poly.area <= 1e-6:
            continue
        records.append({"semantic_type": semantic_type, "rings": polygon_to_3d_rings(poly, to_3D)})

    if not records:
        warnings.append(f"{label}: empty/degenerate boundary, skipped")
    return records


def check_multistory_crossing(wall_records, ground_slab_zs, margin=0.1):
    warnings = []
    for rec in wall_records:
        zs = [pt[2] for ring in rec["rings"] for pt in ring]
        zmin, zmax = min(zs), max(zs)
        for z in ground_slab_zs:
            if zmin + margin < z < zmax - margin:
                warnings.append(
                    f"WallSurface spans z=[{zmin:.2f},{zmax:.2f}] crossing a floor slab at z={z:.2f} "
                    "-- not split (single-wall-per-story splitting not implemented), panel grid will run across the join"
                )
    return warnings


# ------------------------------------------------------------- CityJSON

class VertexPool:
    def __init__(self, precision):
        self.precision = precision
        self.index = {}
        self.verts = []

    def add(self, xyz):
        key = tuple(int(round(c / self.precision)) for c in xyz)
        idx = self.index.get(key)
        if idx is None:
            idx = len(self.verts)
            self.index[key] = idx
            self.verts.append(list(key))
        return idx


def assemble_cityjson(building_name, records, precision):
    pool = VertexPool(precision)
    semantic_type_index = {}
    semantic_surfaces = []
    boundaries = []
    values = []

    for rec in records:
        ring_indices = [[pool.add(pt) for pt in ring] for ring in rec["rings"]]
        boundaries.append(ring_indices)
        sem_type = rec["semantic_type"]
        if sem_type not in semantic_type_index:
            semantic_type_index[sem_type] = len(semantic_surfaces)
            semantic_surfaces.append({"type": sem_type})
        values.append(semantic_type_index[sem_type])

    return {
        "type": "CityJSON",
        "version": "2.0",
        "transform": {"scale": [precision, precision, precision], "translate": [0.0, 0.0, 0.0]},
        "vertices": pool.verts,
        "CityObjects": {
            "Building_1": {
                "type": "Building",
                "attributes": {"name": building_name},
                "geometry": [
                    {
                        "type": "MultiSurface",
                        "lod": "3",
                        "boundaries": boundaries,
                        "semantics": {"surfaces": semantic_surfaces, "values": values},
                    }
                ],
            }
        },
    }


def export_debug_obj(records, path):
    meshes = []
    for rec in records:
        rings = rec["rings"]
        outer = np.asarray(rings[0])
        if len(outer) < 3:
            continue
        try:
            fan = [[0, i, i + 1] for i in range(1, len(outer) - 1)]
            meshes.append(trimesh.Trimesh(vertices=outer, faces=np.asarray(fan), process=False))
        except Exception:
            continue
    if meshes:
        trimesh.util.concatenate(meshes).export(path)


# ------------------------------------------------------------- pipeline

def main():
    parser = argparse.ArgumentParser(description="Extract a LoD3 CityJSON building envelope from an IFC4 file.")
    parser.add_argument("--ifc", required=True, help="Path to the source .ifc file")
    parser.add_argument("--out-dir", required=True, help="Output directory (writes model_raw.json + debug obj)")
    parser.add_argument("--precision", type=float, default=1e-4, help="Vertex quantization / snap grid size, meters")
    parser.add_argument("--angle-tol", type=float, default=15.0,
                        help="Max normal deviation within one planar facet, degrees")
    parser.add_argument("--plane-tol", type=float, default=0.05,
                        help="Max offset between parallel faces treated as the same plane, meters")
    parser.add_argument("--min-facet-area", type=float, default=0.5,
                        help="Ignore outward facets smaller than this, m2 (drops wall end strips)")
    parser.add_argument("--ray-eps", type=float, default=0.05,
                        help="Ray start offset off the facet, meters (avoids self-intersection)")
    parser.add_argument("--ray-max-dist", type=float, default=12.0,
                        help="A facet is internal if its outward ray hits something within this, meters")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ifc_file = ifcopenshell.open(args.ifc)
    declared_unit_scale = ifc_unit.calculate_unit_scale(ifc_file)  # informational only -- ifcopenshell.geom output is already in meters
    settings = make_settings()
    warnings = []

    buildings = ifc_file.by_type("IfcBuilding")
    building_name = buildings[0].Name or Path(args.ifc).stem if buildings else Path(args.ifc).stem

    walls = ifc_file.by_type("IfcWall")
    roofs = ifc_file.by_type("IfcRoof")
    slabs = ifc_file.by_type("IfcSlab")

    ext_walls, int_walls = [], []
    for w in walls:
        is_ext, _ = classify_wall(w, warnings)
        (ext_walls if is_ext else int_walls).append(w)

    roof_bucket, ground_bucket = list(roofs), []
    for s in slabs:
        is_ext, _ = classify_slab(s, warnings)
        (roof_bucket if is_ext else ground_bucket).append(s)

    host_openings = build_host_openings(ifc_file)

    # occluder includes INTERIOR walls too: a room's far side is often a partition, and that's
    # exactly what has to block the ray for an inner leaf to be recognised as internal
    occluder_meshes = [m for m in (create_mesh(w, settings) for w in walls) if m is not None]
    if not occluder_meshes:
        raise SystemExit("No wall geometry could be created from this IFC file.")
    occluder = trimesh.util.concatenate(occluder_meshes)

    records = []
    wall_records_all = []
    for wall in ext_walls:
        recs = build_wall_records(wall, settings, occluder, host_openings, args.angle_tol,
                                  args.plane_tol, args.min_facet_area, args.ray_eps,
                                  args.ray_max_dist, args.precision, warnings)
        wall_records_all.extend(recs)
    records.extend(wall_records_all)

    for roof in roof_bucket:
        records.extend(build_flat_record(roof, settings, "RoofSurface", WORLD_UP, args.angle_tol,
                                         args.plane_tol, args.min_facet_area, args.precision, warnings))
    for slab in ground_bucket:
        records.extend(build_flat_record(slab, settings, "GroundSurface", WORLD_DOWN, args.angle_tol,
                                         args.plane_tol, args.min_facet_area, args.precision, warnings))

    ground_slab_zs = []
    for rec in records:
        if rec["semantic_type"] == "GroundSurface":
            ground_slab_zs.append(np.mean([pt[2] for ring in rec["rings"] for pt in ring]))
    wall_only = [r for r in records if r["semantic_type"] == "WallSurface"]
    warnings.extend(check_multistory_crossing(wall_only, ground_slab_zs))

    cityjson = assemble_cityjson(building_name, records, args.precision)

    raw_path = out_dir / "model_raw.json"
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(cityjson, fh)

    debug_obj_path = out_dir / "model_envelope_debug.obj"
    export_debug_obj(records, debug_obj_path)

    counts = defaultdict(int)
    for rec in records:
        counts[rec["semantic_type"]] += 1

    print(f"Source IFC: {args.ifc}")
    print(f"Declared file unit scale to meters (informational; geometry is already in meters): {declared_unit_scale}")
    print(f"Walls: {len(walls)} total -> {len(ext_walls)} exterior, {len(int_walls)} interior (excluded)")
    print(f"Roofs: {len(roofs)}, Slabs: {len(slabs)} ({len(roof_bucket) - len(roofs)} exterior-slab + {len(roofs)} roof -> roof bucket, {len(ground_bucket)} -> ground bucket)")
    print("Surface counts in output:")
    for sem_type, count in sorted(counts.items()):
        print(f"  {sem_type}: {count}")
    print(f"\nWrote {raw_path}")
    print(f"Wrote debug mesh {debug_obj_path}")

    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings[:50]:
            print(f"  - {w}")
        if len(warnings) > 50:
            print(f"  ... and {len(warnings) - 50} more")


if __name__ == "__main__":
    main()
