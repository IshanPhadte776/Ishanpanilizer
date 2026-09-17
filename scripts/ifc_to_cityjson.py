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
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import unary_union

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element as ifc_element
import ifcopenshell.util.unit as ifc_unit
import networkx as nx
import trimesh
from scipy.spatial import cKDTree


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
            # "SHELL" is Frontenac/Glengarry's Workset convention for envelope walls,
            # alongside Colonel By's "EXTERIORS"/"INTERIORS" -- different firms, same idea.
            if "EXTERIOR" in workset or workset == "SHELL":
                return True, "workset"
            if "INTERIOR" in workset:
                return False, "workset"

    tname = (_type_name(wall) or "").upper()
    if "-EXT-" in tname:
        return True, "typename"
    if "-INT-" in tname:
        return False, "typename"

    # Frontenac/Glengarry use a WE-/WP-/WF- type-mark prefix (Wall Exterior / Partition /
    # Foundation) instead of Colonel By's "-EXT-"/"-INT-" substring convention. WF (Wall
    # Foundation) is deliberately NOT treated as exterior here -- it shows up on far more
    # storeys than just the basement, so it reads as a structural/shear-wall designation
    # rather than a reliable facade signal; leave it falling through to "excluded" below.
    prefix = tname.split(":")[-1].split("-")[0].strip()
    if prefix == "WE":
        return True, "typename_prefix"
    if prefix.startswith("WP"):
        return False, "typename_prefix"

    warnings.append(f"wall {wall.GlobalId} ({tname or 'untyped'}): no Workset/type-name signal, excluding")
    return False, "unknown"


def wall_thickness(wall):
    """Declared wall thickness in meters, from the IFC's own quantities, or None."""
    for props in _psets(wall).values():
        width = props.get("Width")
        if isinstance(width, (int, float)) and width > 0:
            return width / 1000.0  # Qto quantities are in the file's declared mm
    return None


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


def build_storey_glazing_index(ifc_file):
    """Per-storey wall/glazing counts, and wall-id/curtain-wall-id -> storey-id maps,
    straight from real IFC spatial containment (IfcRelContainedInSpatialStructure) -- not
    from storey naming or elevation, neither of which is a reliable basement signal (see
    detect_basement_storeys)."""
    wall_storey = {}
    curtain_wall_storey = {}
    wall_count = defaultdict(int)
    glazing_count = defaultdict(int)
    for rel in ifc_file.by_type("IfcRelContainedInSpatialStructure"):
        storey = rel.RelatingStructure
        if not storey.is_a("IfcBuildingStorey"):
            continue
        for el in rel.RelatedElements:
            if el.is_a("IfcWall"):
                wall_storey[el.id()] = storey.id()
                wall_count[storey.id()] += 1
            elif el.is_a("IfcCurtainWall"):
                curtain_wall_storey[el.id()] = storey.id()
                glazing_count[storey.id()] += 1
            elif el.is_a("IfcWindow"):
                glazing_count[storey.id()] += 1
    return wall_storey, curtain_wall_storey, wall_count, glazing_count


def detect_basement_storeys(ifc_file, wall_count, glazing_count, warnings):
    """Identify below-grade storeys by what's actually built on them, not by storey number
    or elevation sign -- neither holds up across projects. A storey named "LV1" can be a
    building's main occupied ground floor (elevation deeply negative relative to its own
    survey point, e.g. Colonel By) or a genuine basement (Frontenac); a low storey number
    or a negative elevation shows up in both cases, so it can't tell them apart.

    What does tell them apart: a real basement is underground, so it has no facade glazing
    -- zero windows, zero curtain walls -- while every occupied floor above grade has some.
    Walk the storeys bottom-up by elevation and flag a contiguous run of glazing-free
    storeys starting at the lowest one; stop at the first storey that has any windows or
    curtain walls (that's daylight, so it's above grade) or that has no walls at all (an
    empty storey is a modeling artifact, not evidence either way -- don't guess past it).
    """
    storeys = [s for s in ifc_file.by_type("IfcBuildingStorey")
               if "survey point" not in (s.Name or "").lower()]
    if len(storeys) < 2:
        return set()
    storeys.sort(key=lambda s: s.Elevation if s.Elevation is not None else 0.0)

    basement_ids = set()
    for storey in storeys:
        if wall_count[storey.id()] == 0 or glazing_count[storey.id()] > 0:
            break
        basement_ids.add(storey.id())

    if basement_ids and len(basement_ids) < len(storeys):
        names = [s.Name for s in storeys if s.id() in basement_ids]
        warnings.append(
            f"treating storey(s) {', '.join(names)} as below-grade/basement (bottom of the "
            "stack, zero windows or curtain walls) -- excluding their exterior walls from panelization"
        )
    return basement_ids


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


def _shape_to_mesh(shape):
    verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
    faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
    if len(faces) == 0 or len(verts) == 0:
        return None
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.merge_vertices()
    if len(mesh.faces) == 0:
        return None
    return mesh


def build_mesh_cache(ifc_file, settings, classes):
    """Every element's mesh, computed once, up front, via ifcopenshell's own multi-threaded
    iterator -- not the one create_shape() call per element the rest of this file used to
    make. Profiled: create_shape is 81% of total extraction time (single-threaded, ~82ms/
    element), and the per-element approach separately computed the SAME exterior wall's
    geometry twice over (once for the occluder pass over all walls, again in prepare_wall
    for that wall specifically) -- this cache removes both costs, since every caller now
    shares one lookup keyed by element id instead of each calling create_shape on its own.
    Measured 2.1-2.4x faster than the old approach even before removing that duplication.
    """
    cache = {}
    iterator = ifcopenshell.geom.iterator(settings, ifc_file, num_threads=os.cpu_count() or 4, include=classes)
    if not iterator.initialize():
        return cache
    while True:
        shape = iterator.get()
        mesh = _shape_to_mesh(shape)
        if mesh is not None:
            cache[shape.id] = mesh
        if not iterator.next():
            break
    return cache


def create_mesh(element, settings, cache=None):
    # ifcopenshell.geom (with USE_WORLD_COORDS) returns coordinates already
    # normalized to meters regardless of the file's declared length unit --
    # verified empirically against this file's own mm-declared Qto data.
    if cache is not None:
        cached = cache.get(element.id())
        if cached is not None:
            return cached.copy()
        # not in the cache (e.g. a class build_mesh_cache wasn't asked to include) --
        # fall through to computing it directly rather than assuming "no geometry"
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
    except Exception:
        return None
    return _shape_to_mesh(shape)


# ------------------------------------------------------------ face select

def build_occluder(walls, settings, warnings, mesh_cache=None, is_ext_by_id=None):
    """Concatenated wall geometry plus, per face, which element it came from and whether
    that element is an interior wall. Ray hits are only interpretable with those labels.

    `is_ext_by_id` reuses classify_wall's result from main()'s own ext/int/finish sort
    instead of calling it again here for the same 6,000+ walls -- profiled at ~8s of
    redundant pset lookups on Glengarry (12,478 classify_wall calls for 6,401 elements is
    exactly 2x, one from main(), one from here, before this fix).
    """
    meshes, owners, interior, ids = [], [], [], []
    for wall in walls:
        mesh = create_mesh(wall, settings, cache=mesh_cache)
        if mesh is None:
            continue
        owners.append(np.full(len(mesh.faces), len(ids), dtype=np.int64))
        if is_ext_by_id is not None and wall.id() in is_ext_by_id:
            is_ext = is_ext_by_id[wall.id()]
        else:
            is_ext = classify_wall(wall, warnings)[0]
        interior.append(not is_ext)
        ids.append(wall.id())
        meshes.append(mesh)

    if not meshes:
        return None
    return {
        "mesh": trimesh.util.concatenate(meshes),
        "face_owner": np.concatenate(owners),
        "is_interior": np.asarray(interior, dtype=bool),
        "ids": np.asarray(ids, dtype=np.int64),
    }


def facet_openness(mesh, occluder, facets, eps, max_dist, max_samples=24):
    """Per facet, the mean unobstructed distance ahead of it, normalised to [0, 1].

    A plain blocked/not-blocked vote can't separate a wall facing a 3m room from one facing
    another wing 8m away -- both are "blocked". Keeping the distance makes them comparable.
    """
    if occluder is None or not facets:
        return np.ones(len(facets))

    tri_centroids = mesh.triangles.mean(axis=1)
    origins, directions, owner = [], [], []
    for index, facet in enumerate(facets):
        faces = np.asarray(facet["faces"])
        if len(faces) > max_samples:
            faces = faces[np.linspace(0, len(faces) - 1, max_samples).astype(int)]
        for face in faces:
            origins.append(tri_centroids[face] + facet["normal"] * eps)
            directions.append(facet["normal"])
            owner.append(index)

    origins = np.asarray(origins)
    owner = np.asarray(owner)
    free = np.full(len(origins), max_dist, dtype=float)

    locations, index_ray, _ = occluder["mesh"].ray.intersects_location(
        ray_origins=origins, ray_directions=np.asarray(directions), multiple_hits=False
    )
    for location, ray in zip(locations, index_ray):
        free[ray] = min(free[ray], np.linalg.norm(location - origins[ray]))

    return np.array([
        free[owner == index].mean() / max_dist if (owner == index).any() else 1.0
        for index in range(len(facets))
    ])


def select_outward_facets(mesh, occluder, facets, eps, max_dist, min_open, angle_tol_deg):
    """Of each opposing pair of facets on the same wall plane, keep the one facing outward.

    Comparing the two sides against each other is what makes this robust: absolute
    thresholds fail because room depth and courtyard width overlap, but the outer leaf of a
    given wall always has more clear space ahead of it than its own inner leaf does.
    """
    if not facets:
        return facets

    scores = facet_openness(mesh, occluder, facets, eps, max_dist)
    cos_tol = np.cos(np.radians(angle_tol_deg))

    kept, consumed = [], set()
    for index in np.argsort(-scores):
        if index in consumed:
            continue
        facet = facets[index]
        to_2D = trimesh.geometry.plane_transform([0.0, 0.0, 0.0], facet["normal"])
        hull = _facet_hull(mesh, facet, to_2D)

        paired = False
        for other_index, other in enumerate(facets):
            if other_index == index or other_index in consumed:
                continue
            if facet["normal"] @ other["normal"] > -cos_tol:
                continue
            other_hull = _facet_hull(mesh, other, to_2D)
            overlap = hull.intersection(other_hull).area
            if overlap > 0.5 * min(hull.area, other_hull.area):
                consumed.add(other_index)
                paired = True

        consumed.add(index)
        if paired or scores[index] >= min_open:
            kept.append(facet)
    return kept


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


def split_by_connectivity(mesh, face_indices):
    """Split a face set into its spatially-connected pieces (sharing an edge, transitively).

    Same plane does not mean same physical surface: two separate wall stubs, or a bent wall
    whose two ends happen to land back on the same infinite plane, can share a normal and
    offset while never touching. Grouping them anyway hands `mesh.outline()` a disconnected
    face set, which produces one self-crossing "boundary" zig-zagging between both pieces --
    silently, with a plausible-looking area, rather than an error.
    """
    face_set = set(int(f) for f in face_indices)
    graph = nx.Graph()
    graph.add_nodes_from(int(f) for f in face_indices)
    for a, b in mesh.face_adjacency:
        a, b = int(a), int(b)
        if a in face_set and b in face_set:
            graph.add_edge(a, b)
    return [np.array(sorted(component), dtype=np.int64) for component in nx.connected_components(graph)]


def cluster_planar_facets(mesh, angle_tol_deg, dist_tol):
    """Group a mesh's faces into planar, connected patches (same normal, same plane offset,
    and physically touching).

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

        for component in split_by_connectivity(mesh, members):
            assigned[component] = len(clusters)
            weights = areas[component]
            normal = np.average(normals[component], axis=0, weights=weights)
            norm = np.linalg.norm(normal)
            normal = seed_normal if norm < 1e-12 else normal / norm
            clusters.append({
                "faces": component,
                "normal": normal,
                "origin": np.average(centroids[component], axis=0, weights=weights),
                "area": float(weights.sum()),
            })

    return clusters


# ------------------------------------------------------------- boundaries

def polygon_from_closed_loops(loops):
    """Rebuild an outer-with-holes polygon from raw closed boundary loops.

    trimesh's own `polygons_full` nesting gives up when a facet carries duplicate coincident
    loops -- which happens when an element ships two stacked copies of the same sheet (the
    856m2 CC-Roof slab in this model does exactly that). Here the largest loop is the
    exterior, anything strictly inside it is a hole, and a near-identical twin is a
    duplicate and dropped.
    """
    cleaned = []
    for loop in loops:
        poly = loop if loop.is_valid else shapely.make_valid(loop)
        poly = largest_polygon(poly)
        if poly is not None and poly.area > 0:
            cleaned.append(poly)
    if not cleaned:
        return None

    cleaned.sort(key=lambda p: -p.area)
    outer = cleaned[0]
    holes = [p for p in cleaned[1:] if p.area < outer.area * 0.99 and outer.contains(p)]
    if holes:
        outer = outer.difference(unary_union(holes))
    return largest_polygon(outer)


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

    # Reconstruct from the raw closed loops rather than trusting `polygons_full`. When an
    # element ships duplicate coincident loops -- which several do here -- the enclosure tree
    # silently drops BOTH copies of the outer boundary and returns only the window holes, so
    # `polygons_full` is non-empty but wrong, and picking its largest member yields a window.
    try:
        poly = polygon_from_closed_loops(planar.polygons_closed)
    except Exception:
        poly = None
    if poly is not None:
        return poly, to_3D

    polys = planar.polygons_full
    if polys:
        return max(polys, key=lambda p: p.area), to_3D
    return None, None


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


def opening_footprint(mesh, to_2D_transform):
    pts_h = np.hstack([mesh.vertices, np.ones((len(mesh.vertices), 1))])
    pts2d = (to_2D_transform @ pts_h.T).T[:, :2]
    hull = MultiPoint(pts2d).convex_hull
    if hull.geom_type != "Polygon" or hull.area <= 1e-8:
        return None
    return hull


def dominant_normal(mesh):
    """The window/door's own primary face direction, front and back folded together.

    Connectivity-based clustering (as used for walls) is the wrong tool here: mullions and
    separate panes make a small window fragment into many disconnected pieces with no single
    dominant one. A plain area-weighted average of all face normals is also wrong, since a
    thin box's near-equal front and back faces point opposite ways and largely cancel out.
    Folding every normal into the same hemisphere as the single largest triangle's normal
    before averaging avoids both problems -- sign doesn't matter here since callers only
    check alignment via `abs(dot)`.
    """
    normals = mesh.face_normals
    areas = mesh.area_faces
    reference = normals[np.argmax(areas)]
    folded = np.where((normals @ reference)[:, None] < 0, -normals, normals)
    normal = np.average(folded, axis=0, weights=areas)
    norm = np.linalg.norm(normal)
    return normal / norm if norm > 1e-8 else None


# ------------------------------------------------------------- assembly

def merge_facets(facets):
    """Combine a wall's separately-clustered facets into one approximate plane.

    Used only as a same-wall fallback (see match_wall_openings) for a continuously curved
    wall whose window is wide relative to the curve's radius: no single flat facet from the
    normal clustering ever captures most of such a window, since it genuinely straddles
    several of them. This never reaches across walls, unlike a global cross-wall match --
    that was tried and reverted because it let unrelated openings bind to coincidentally
    plausible geometry elsewhere in the building.
    """
    faces = np.concatenate([f["faces"] for f in facets])
    areas = np.array([f["area"] for f in facets])
    normal = np.average([f["normal"] for f in facets], axis=0, weights=areas)
    norm = np.linalg.norm(normal)
    normal = facets[0]["normal"] if norm < 1e-12 else normal / norm
    origin = np.average([f["origin"] for f in facets], axis=0, weights=areas)
    return {"faces": faces, "normal": normal, "origin": origin, "area": float(areas.sum())}


def merged_match_region(mesh, facets, grid_size):
    """One filled 2D region covering this wall's whole outward side, in a single common
    frame. Used as a matching target only -- never as the emitted wall surface.

    Built by projecting each facet's own vertices into a shared plane and unioning their
    hulls, NOT by re-tracing an outline over the combined face set: a wall whose door or
    window is voided out of its mesh has an outer face that is genuinely two disconnected
    columns either side of the opening, and tracing one outline across disconnected pieces
    produces a self-crossing garbage boundary (the same failure connectivity-splitting was
    added to prevent). Filling across the gaps is the whole point here -- an opening that
    sits *in* the gap is exactly what we're trying to place.
    """
    merged = merge_facets(facets)
    to_2D = trimesh.geometry.plane_transform(merged["origin"], merged["normal"])

    parts = []
    for facet in facets:
        pts = mesh.vertices[np.unique(mesh.faces[facet["faces"]].ravel())]
        homogeneous = np.hstack([pts, np.ones((len(pts), 1))])
        hull = MultiPoint((to_2D @ homogeneous.T).T[:, :2]).convex_hull
        if hull.geom_type == "Polygon" and hull.area > 0:
            parts.append(hull)
    if not parts:
        return None

    region = clean_polygon(unary_union(parts), grid_size)
    if region is None or region.area <= 1e-6:
        return None
    return {
        "poly": Polygon(region.exterior), "to_2D": to_2D, "to_3D": np.linalg.inv(to_2D),
        "normal": merged["normal"], "cuts": [],
    }


# --------------------------------------------------------- curtain walls

def curtain_wall_children_mesh(curtain_wall, settings, mesh_cache=None):
    """Concatenate geometry from a curtain wall's real geometry carriers.

    IfcCurtainWall itself never has its own shape -- in this file all 61 are pure
    IsDecomposedBy containers over IfcPlate (glazing infill) and IfcMember (mullions/
    transoms). Walk the decomposition (it can nest) and collect those two types.
    """
    children = []
    stack = list(getattr(curtain_wall, "IsDecomposedBy", []) or [])
    seen = set()
    while stack:
        rel = stack.pop()
        for child in rel.RelatedObjects:
            if child.id() in seen:
                continue
            seen.add(child.id())
            if child.is_a("IfcPlate") or child.is_a("IfcMember"):
                children.append(child)
            stack.extend(getattr(child, "IsDecomposedBy", []) or [])

    meshes = [m for m in (create_mesh(c, settings, cache=mesh_cache) for c in children) if m is not None]
    if not meshes:
        return None
    return trimesh.util.concatenate(meshes)


def curtain_wall_window_records_from_mesh(mesh, label, angle_tol_deg, plane_tol, min_facet_area, grid_size):
    """The CPU-heavy half of curtain-wall processing: clustering the merged plate/member
    mesh into facade planes and filling each into one 'Window'. Pure numpy/shapely/trimesh,
    no ifcopenshell dependency -- split out from build_curtain_wall_window_records
    specifically so it can run in a worker process (see main()): profiled as the single
    biggest cost on a curtain-wall-heavy building (39.5s of ~70s total on Glengarry's 320
    curtain walls, once the create_shape bottleneck elsewhere was already fixed).

    The individual IfcPlate/IfcMember pieces don't physically touch each other, so
    cluster_planar_facets's connectivity split -- correct for a solid wall -- would hand
    back hundreds of tiny disconnected facets here instead of one plane. Re-group those by
    plane, then reuse merged_match_region's hull-union trick (built for the same
    "disconnected pieces on one plane" problem on wall openings) to fill across the gaps.

    Returns (records, warnings) rather than appending to a shared warnings list, since a
    list mutated inside a worker process wouldn't be visible to the caller's own copy.
    """
    warnings = []
    facets = [f for f in cluster_planar_facets(mesh, angle_tol_deg, plane_tol) if f["area"] >= 0.01]
    if not facets:
        warnings.append(f"{label}: no usable facets, skipped")
        return [], warnings

    cos_tol = np.cos(np.radians(angle_tol_deg))
    groups = []
    for facet in sorted(facets, key=lambda f: -f["area"]):
        for group in groups:
            ref = group[0]
            if ref["normal"] @ facet["normal"] > cos_tol and \
                    abs((facet["origin"] - ref["origin"]) @ ref["normal"]) < plane_tol:
                group.append(facet)
                break
        else:
            groups.append([facet])

    records = []
    for group in groups:
        region = merged_match_region(mesh, group, grid_size)
        if region is None or region["poly"].area < min_facet_area:
            continue
        records.append({"semantic_type": "Window", "rings": polygon_to_3d_rings(region["poly"], region["to_3D"])})

    if not records:
        warnings.append(f"{label}: empty/degenerate boundary, skipped")
    return records, warnings


def _curtain_wall_task(args):
    """Top-level (picklable) entry point for a worker process -- see main()'s use of
    ProcessPoolExecutor over curtain_wall_window_records_from_mesh."""
    label, mesh, angle_tol_deg, plane_tol, min_facet_area, grid_size = args
    return curtain_wall_window_records_from_mesh(mesh, label, angle_tol_deg, plane_tol, min_facet_area, grid_size)


def build_curtain_wall_window_records(curtain_wall, settings, angle_tol_deg, plane_tol,
                                       min_facet_area, grid_size, warnings, mesh_cache=None):
    """Emit one merged 'Window' surface per facade plane of a curtain wall, instead of its
    real mullion/panel grid -- per the user's call: a curtain wall becomes one giant window,
    so the panelizer (which only targets WallSurface) leaves it alone entirely.

    Sequential single-curtain-wall convenience wrapper around curtain_wall_children_mesh
    (ifcopenshell-dependent, must run in-process) + curtain_wall_window_records_from_mesh
    (the parallelizable part). main() calls those two directly instead of this, to batch
    the mesh-gathering here and dispatch the heavy part across a process pool.
    """
    label = f"IfcCurtainWall {curtain_wall.GlobalId}"
    mesh = curtain_wall_children_mesh(curtain_wall, settings, mesh_cache=mesh_cache)
    if mesh is None:
        warnings.append(f"{label}: no plate/member geometry, skipped")
        return []
    records, new_warnings = curtain_wall_window_records_from_mesh(
        mesh, label, angle_tol_deg, plane_tol, min_facet_area, grid_size,
    )
    warnings.extend(new_warnings)
    return records


def build_facet_data(mesh, facets, grid_size, warnings, label):
    """Boundary polygons (own holes stripped) for a set of already-selected facets."""
    facet_data = []
    for facet in facets:
        poly, to_3D = extract_boundary_polygon(mesh, facet["faces"], facet["normal"], facet["origin"])
        poly = clean_polygon(poly, grid_size)
        if poly is None or poly.area <= 1e-6:
            warnings.append(f"{label}: dropped a {facet['area']:.1f} m2 facet, boundary came back empty")
            continue

        # This wall's mesh often already has its openings voided out by ifcopenshell (this
        # file's Tessellation-bodied walls do), so `poly` can already carry holes exactly
        # where a window sits -- meaning the window's own footprint would show ~0% overlap
        # against it, not because anything is misaligned but because a hole has no filled
        # area to overlap by definition. Work from the solid outer boundary and let the
        # matching + subtraction below recompute openings ourselves, consistently.
        poly = Polygon(poly.exterior)

        facet_data.append({
            "poly": poly, "to_3D": to_3D, "to_2D": np.linalg.inv(to_3D),
            "normal": facet["normal"], "cuts": [],
        })
    return facet_data


def match_opening(filling_mesh, filling_normal, facet_data):
    """Best (coverage, footprint, facet) for one opening against a set of facets, or None.

    Assign to whichever facet actually contains most of the opening's own footprint -- not
    just whichever it happens to touch first. A window can graze an unrelated facet at a
    sliver; projecting its full footprint onto that facet's (wrong) plane produces a wildly
    oversized, garbled shape, not just a slightly-off one, since it's an oblique projection
    of a 3D solid. Requiring most of the opening's own area inside the chosen facet is what
    rejects that, rather than any nonzero-overlap threshold.

    That alone isn't enough once facets are split by connectivity: a small facet running
    roughly perpendicular to the window (a jamb return, a reveal side) can still score a
    deceptively high coverage ratio (tiny denominator), and projecting the window edge-on
    through it collapses its true height into a paper-thin sliver. Gate on the window's own
    face direction actually facing the same way as the facet first.
    """
    best = None
    for fd in facet_data:
        if filling_normal is not None and abs(float(filling_normal @ fd["normal"])) < 0.6:
            continue
        footprint = opening_footprint(filling_mesh, fd["to_2D"])
        if footprint is None or footprint.area <= 1e-9:
            continue
        coverage = footprint.intersection(fd["poly"]).area / footprint.area
        if best is None or coverage > best[0]:
            best = (coverage, footprint, fd)
    return best


def prepare_wall(wall, settings, occluder, angle_tol_deg, plane_tol, min_facet_area,
                 ray_eps, ray_max_dist, min_open, grid_size, warnings, mesh_cache=None):
    """This wall's mesh, its facets (pre-merge, kept for the same-wall fallback), and their
    boundary polygons. Geometry only -- no opening matching happens here."""
    label = f"wall {wall.GlobalId}"
    mesh = create_mesh(wall, settings, cache=mesh_cache)
    if mesh is None:
        warnings.append(f"{label}: no geometry, skipped")
        return None

    clusters = cluster_planar_facets(mesh, angle_tol_deg, plane_tol)

    # keep the roughly-vertical, outward-facing patches: that drops the top/bottom caps,
    # the inner leaf, and the thin end strips that aren't claddable facade
    facets = [c for c in clusters if abs(c["normal"][2]) < 0.7 and c["area"] >= min_facet_area]
    facets = select_outward_facets(mesh, occluder, facets, ray_eps, ray_max_dist, min_open, angle_tol_deg)
    facets = dedupe_layered_facets(mesh, facets, angle_tol_deg)
    if not facets:
        warnings.append(f"{label}: no outward-facing vertical facet, skipped")
        return None

    facet_data = build_facet_data(mesh, facets, grid_size, warnings, label)
    if not facet_data:
        warnings.append(f"{label}: no usable facet boundary, skipped")
        return None

    return {"wall": wall, "label": label, "mesh": mesh, "facets": facets, "facet_data": facet_data}


def match_wall_openings(info, host_openings, settings, grid_size, warnings, mesh_cache=None):
    """Match this wall's own declared openings against its own facets (fine, then a
    same-wall merged fallback for curved walls). Returns the matched Window/Door records,
    plus (filling, filling_mesh, filling_normal) for any that still failed -- those get one
    more try against nearby walls only, in match_nearby_fallback(), since IFC's void/fill
    host relationship isn't always trustworthy (verified: a window whose declared host wall
    doesn't geometrically contain it at all, while a neighboring wall sharing the same
    corner does, at 100%)."""
    openings = host_openings.get(info["wall"].id(), [])
    facet_data = info["facet_data"]

    filling_cache = {}
    matches = {}
    unmatched_indices = []
    for index, (_opening, filling) in enumerate(openings):
        filling_mesh = create_mesh(filling, settings, cache=mesh_cache)
        if filling_mesh is None:
            continue
        filling_normal = dominant_normal(filling_mesh)
        filling_cache[index] = (filling_mesh, filling_normal)

        best = match_opening(filling_mesh, filling_normal, facet_data)
        if best is not None and best[0] >= 0.5:
            matches[index] = best
        else:
            unmatched_indices.append(index)

    # Same-wall-only fallback, for two cases where no single facet can hold the opening:
    #   - a door (or full-height window) voided out of the wall mesh leaves the outer face as
    #     two disconnected columns, and the opening sits in the gap between them
    #   - a continuously curved wall's window is wide enough relative to the curve radius to
    #     straddle several flat facet approximations at once
    # Both are placed against a filled region spanning the wall's whole outward side. The
    # fine facets stay as the emitted wall surfaces -- the region is a matching target only.
    # Runs for single-facet walls too, not just multi-facet ones: a door reaching the wall's
    # base cuts a *notch*, not a hole, and a notch is part of the exterior ring -- so
    # stripping interior rings doesn't fill it and the door matches nothing. The region's
    # per-facet convex hull does fill it.
    region = None
    if unmatched_indices:
        region = merged_match_region(info["mesh"], info["facets"], grid_size)
        if region is not None:
            recovered = []
            for index in list(unmatched_indices):
                filling_mesh, filling_normal = filling_cache[index]
                best = match_opening(filling_mesh, filling_normal, [region])
                if best is not None and best[0] >= 0.5:
                    matches[index] = best
                    recovered.append(index)

            if recovered:
                unmatched_indices = [i for i in unmatched_indices if i not in recovered]
                warnings.append(
                    f"{info['label']}: {len(recovered)} opening(s) placed against a wall-wide merged "
                    "region (opening spans, or falls between, this wall's flat sub-facets)"
                )

    records = []
    unmatched = []
    for index, (_opening, filling) in enumerate(openings):
        if index in matches:
            _coverage, footprint, fd = matches[index]
            sem = "Window" if filling.is_a("IfcWindow") else "Door"
            trimmed = clean_polygon(footprint, grid_size) or footprint
            records.append({"semantic_type": sem, "rings": polygon_to_3d_rings(trimmed, fd["to_3D"]),
                            "is_basement": info["is_basement"]})

            if fd is region:
                # Placed via the merged region, so cut it from whichever fine facets it
                # genuinely overlaps. Usually a no-op: the reason it needed the region at all
                # is that the mesh already had this opening voided out.
                filling_mesh, _normal = filling_cache[index]
                for ffd in facet_data:
                    projected = opening_footprint(filling_mesh, ffd["to_2D"])
                    if projected is not None and projected.intersection(ffd["poly"]).area > 1e-6:
                        ffd["cuts"].append(projected)
            else:
                fd["cuts"].append(footprint)
        elif index in filling_cache:
            filling_mesh, filling_normal = filling_cache[index]
            unmatched.append((filling, filling_mesh, filling_normal))

    return records, unmatched


def match_nearby_fallback(info, unmatched, wall_infos, grid_size, warnings, max_dist=3.0):
    """Last resort for openings that failed their own wall entirely: check only walls whose
    mesh actually comes within `max_dist` of the opening, never the whole building. This is
    what makes it safe where checking every wall wasn't -- a bounded neighborhood can't
    produce the coincidental-but-wrong matches elsewhere in the building that the full
    cross-wall version did (tried, reverted after visibly making other openings worse).

    The proximity test is real point-to-point distance (via a KD-tree on each candidate
    wall's vertices), not axis-aligned bounding-box overlap. Verified the difference matters:
    an AABB-expansion check let two walls 5.8-8.0m apart through as "~3.6m apart" -- both
    onto walls in the largest 10% by area, the exact shape of a false positive (a 2m2 window
    coincidentally landing "inside" a 70-280m2 polygon's 2D extent is not evidence of a real
    relationship). Genuine same-building-element pairs measured here sit at a true 2.4m, with
    a wide, clean gap before the next-closest spurious one at 5.8m -- so max_dist stays a
    tight, real distance, not a generous proxy.
    """
    records = []
    for filling, filling_mesh, filling_normal in unmatched:
        probe = cKDTree(filling_mesh.vertices)
        best = None
        best_other = None
        for other in wall_infos:
            if other is info:
                continue
            # cheap prefilter before the real (more expensive) nearest-neighbor query
            lo = other["mesh"].bounds[0] - max_dist
            hi = other["mesh"].bounds[1] + max_dist
            if np.any(filling_mesh.bounds[1] < lo) or np.any(filling_mesh.bounds[0] > hi):
                continue
            true_dist = probe.query(other["mesh"].vertices)[0].min()
            if true_dist > max_dist:
                continue
            candidate = match_opening(filling_mesh, filling_normal, other["facet_data"])
            if candidate is not None and (best is None or candidate[0] > best[0]):
                best, best_other = candidate, other

        if best is not None and best[0] >= 0.5:
            coverage, footprint, fd = best
            fd["cuts"].append(footprint)
            sem = "Window" if filling.is_a("IfcWindow") else "Door"
            trimmed = clean_polygon(footprint, grid_size) or footprint
            # tagged by the wall it actually landed in (best_other), not the one that
            # declared it -- that's the wall whose facet/storey it's physically cut into
            records.append({"semantic_type": sem, "rings": polygon_to_3d_rings(trimmed, fd["to_3D"]),
                            "is_basement": best_other["is_basement"]})
            warnings.append(
                f"{filling.is_a()} {filling.GlobalId}: declared host ({info['label']}) doesn't geometrically "
                f"contain it, but neighboring {best_other['label']} does ({coverage:.0%}) -- IFC void/fill "
                "relation looks wrong for this element, used the geometric match instead"
            )
        else:
            warnings.append(f"{info['label']}: {filling.is_a()} {filling.GlobalId} could not be matched to any nearby facet, dropped")

    return records


def finalize_walls(wall_infos, grid_size, warnings):
    """Subtract each wall's matched openings and emit its WallSurface record(s)."""
    records = []
    for info in wall_infos:
        for fd in info["facet_data"]:
            wall_poly = fd["poly"]
            if fd["cuts"]:
                cut = clean_polygon(wall_poly.difference(unary_union(fd["cuts"])), grid_size)
                if cut is not None and cut.area > 1e-6:
                    wall_poly = cut
                else:
                    warnings.append(f"{info['label']}: opening subtraction emptied a facet, keeping it uncut")
            records.append({"semantic_type": "WallSurface", "rings": polygon_to_3d_rings(wall_poly, fd["to_3D"]),
                            "is_basement": info["is_basement"]})
        if len(info["facet_data"]) > 1:
            warnings.append(f"{info['label']}: multi-segment wall split into {len(info['facet_data'])} facets")
    return records


def build_flat_record(element, settings, semantic_type, preferred_dir, angle_tol_deg, plane_tol,
                      min_facet_area, grid_size, warnings, mesh_cache=None):
    label = f"{element.is_a()} {element.GlobalId}"
    mesh = create_mesh(element, settings, cache=mesh_cache)
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
        is_basement = bool(rec.get("is_basement"))
        # keyed on (type, is_basement) so a below-grade WallSurface gets its own semantic
        # surface entry distinct from a regular one, instead of collapsing together
        key = (sem_type, is_basement)
        if key not in semantic_type_index:
            semantic_type_index[key] = len(semantic_surfaces)
            surface = {"type": sem_type}
            if is_basement:
                surface["is_basement"] = True
            semantic_surfaces.append(surface)
        values.append(semantic_type_index[key])

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
    parser.add_argument("--min-wall-thickness", type=float, default=0.05,
                        help="Exclude exterior walls thinner than this, meters (drops paint/finish layers "
                             "modeled as their own coplanar walls, which bury the openings behind them)")
    parser.add_argument("--min-open", type=float, default=0.2,
                        help="Fraction of a facet's sampled rays that must see open air to keep it")
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
    curtain_walls = ifc_file.by_type("IfcCurtainWall")

    wall_storey, curtain_wall_storey, storey_wall_count, storey_glazing_count = build_storey_glazing_index(ifc_file)
    basement_storey_ids = detect_basement_storeys(ifc_file, storey_wall_count, storey_glazing_count, warnings)

    # Finish layers (e.g. this file's 13 "EXT-WL_05mm-PAINT" walls) are modeled as their own
    # IfcWall elements sitting coplanar on top of the real wall. They host no openings, so
    # they render as solid panels burying the windows cut into the wall behind them, and
    # they double-count facade area. A 5mm "wall" is paint, not a claddable surface.
    #
    # Below-grade walls are TAGGED, not dropped: they still go through the same extraction
    # (finalize_walls stamps is_basement on their records below), so the panelizer can
    # toggle them in or out (default off) instead of them being unrecoverably absent.
    ext_walls, int_walls, finish_walls = [], [], []
    basement_wall_ids = set()
    is_ext_by_id = {}
    for w in walls:
        is_ext, _ = classify_wall(w, warnings)
        is_ext_by_id[w.id()] = is_ext
        if not is_ext:
            int_walls.append(w)
            continue
        thickness = wall_thickness(w)
        if thickness is not None and thickness < args.min_wall_thickness:
            finish_walls.append(w)
            continue
        ext_walls.append(w)
        if wall_storey.get(w.id()) in basement_storey_ids:
            basement_wall_ids.add(w.id())

    if finish_walls:
        warnings.append(
            f"excluded {len(finish_walls)} finish-layer wall(s) thinner than "
            f"{args.min_wall_thickness * 1000:.0f}mm (coplanar duplicates that hide real openings)"
        )

    roof_bucket, ground_bucket = list(roofs), []
    for s in slabs:
        is_ext, _ = classify_slab(s, warnings)
        (roof_bucket if is_ext else ground_bucket).append(s)

    # Curtain walls become one giant Window per facade plane (see build_curtain_wall_window_records)
    # rather than real panels -- classify_wall works unchanged since it's generic over psets/type-name.
    ext_curtain_walls, int_curtain_walls, basement_curtain_walls = [], [], []
    for cw in curtain_walls:
        is_ext, _ = classify_wall(cw, warnings)
        if not is_ext:
            int_curtain_walls.append(cw)
        elif curtain_wall_storey.get(cw.id()) in basement_storey_ids:
            basement_curtain_walls.append(cw)
        else:
            ext_curtain_walls.append(cw)

    host_openings = build_host_openings(ifc_file)

    # Every element this file ever calls create_mesh() on, computed once via ifcopenshell's
    # multi-threaded iterator instead of one single-threaded create_shape() call apiece --
    # see build_mesh_cache. This is also what removes the old duplicate work where an
    # exterior wall's geometry was built once for the occluder pass (all walls) and again
    # in prepare_wall (that wall specifically): both now hit the same cache entry.
    mesh_cache = build_mesh_cache(
        ifc_file, settings,
        ["IfcWall", "IfcRoof", "IfcSlab", "IfcWindow", "IfcDoor", "IfcPlate", "IfcMember"],
    )

    # occluder includes INTERIOR walls too: a room's far side is often a partition, and that's
    # exactly what has to block the ray for an inner leaf to be recognised as internal
    occluder = build_occluder(walls, settings, warnings, mesh_cache=mesh_cache, is_ext_by_id=is_ext_by_id)
    if occluder is None:
        raise SystemExit("No wall geometry could be created from this IFC file.")

    records = []

    wall_infos = []
    for wall in ext_walls:
        info = prepare_wall(wall, settings, occluder, args.angle_tol, args.plane_tol,
                            args.min_facet_area, args.ray_eps, args.ray_max_dist,
                            args.min_open, args.precision, warnings, mesh_cache=mesh_cache)
        if info is not None:
            info["is_basement"] = wall.id() in basement_wall_ids
            wall_infos.append(info)

    all_unmatched = []  # (info, [(filling, filling_mesh, filling_normal), ...])
    for info in wall_infos:
        recs, unmatched = match_wall_openings(info, host_openings, settings, args.precision, warnings,
                                              mesh_cache=mesh_cache)
        records.extend(recs)
        if unmatched:
            all_unmatched.append((info, unmatched))

    for info, unmatched in all_unmatched:
        records.extend(match_nearby_fallback(info, unmatched, wall_infos, args.precision, warnings))

    wall_records_all = finalize_walls(wall_infos, args.precision, warnings)
    records.extend(wall_records_all)

    for roof in roof_bucket:
        records.extend(build_flat_record(roof, settings, "RoofSurface", WORLD_UP, args.angle_tol,
                                         args.plane_tol, args.min_facet_area, args.precision, warnings,
                                         mesh_cache=mesh_cache))
    for slab in ground_bucket:
        records.extend(build_flat_record(slab, settings, "GroundSurface", WORLD_DOWN, args.angle_tol,
                                         args.plane_tol, args.min_facet_area, args.precision, warnings,
                                         mesh_cache=mesh_cache))
    # Curtain walls, in two phases. Phase 1 (here, sequential): gather each one's merged
    # plate/member mesh -- needs the live ifcopenshell file, so has to run in this process,
    # but is fast now that mesh_cache means no create_shape call happens in this step.
    curtain_wall_tasks = []
    for cw in ext_curtain_walls:
        label = f"IfcCurtainWall {cw.GlobalId}"
        mesh = curtain_wall_children_mesh(cw, settings, mesh_cache=mesh_cache)
        if mesh is None:
            warnings.append(f"{label}: no plate/member geometry, skipped")
            continue
        curtain_wall_tasks.append((label, mesh, args.angle_tol, args.plane_tol, args.min_facet_area, args.precision))

    # Phase 2: the clustering/merging work itself is pure numpy/shapely/trimesh with no
    # ifcopenshell dependency (unlike everything else in this file), so it can run across a
    # process pool. Profiled as the single biggest remaining cost on a curtain-wall-heavy
    # building (39.5s of ~70s total on Glengarry's 320 curtain walls) -- worth a one-shot
    # pool's spawn cost even in this single CLI invocation, unlike panelizer.py's per-wall
    # tasks (a few ms each), where a fresh pool was a net loss: each curtain wall here
    # averages ~120ms of real work, so the spawn cost amortizes easily.
    if len(curtain_wall_tasks) > 8:
        workers = os.cpu_count() or 4
        chunksize = max(1, len(curtain_wall_tasks) // (workers * 4))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for recs, new_warnings in pool.map(_curtain_wall_task, curtain_wall_tasks, chunksize=chunksize):
                records.extend(recs)
                warnings.extend(new_warnings)
    else:
        for label, mesh, a_tol, p_tol, min_area, grid in curtain_wall_tasks:
            recs, new_warnings = curtain_wall_window_records_from_mesh(mesh, label, a_tol, p_tol, min_area, grid)
            records.extend(recs)
            warnings.extend(new_warnings)

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
    print(f"Walls: {len(walls)} total -> {len(ext_walls)} exterior, {len(int_walls)} interior (excluded); "
          f"{len(basement_wall_ids)} of those exterior walls are below-grade (tagged is_basement, "
          f"excluded from panelization unless include_basement is set)")
    print(f"Roofs: {len(roofs)}, Slabs: {len(slabs)} ({len(roof_bucket) - len(roofs)} exterior-slab + {len(roofs)} roof -> roof bucket, {len(ground_bucket)} -> ground bucket)")
    print(f"Curtain walls: {len(curtain_walls)} total -> {len(ext_curtain_walls)} exterior (emitted as one "
          f"Window each, per facade plane), {len(int_curtain_walls)} interior (excluded), "
          f"{len(basement_curtain_walls)} below-grade (excluded)")
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
