import json
import os
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import open3d as o3d
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box
from shapely.ops import triangulate, unary_union

try:
    import mapbox_earcut as earcut
except ImportError:
    earcut = None


DEFAULT_CONFIG = {
    "input_json": "input/outputID2/Output/lod3.json",
    "output_json": "output/2_panels.json",
    "selected_building_indices": [0],
    "panel_width": 1.2,
    "panel_height": 2.4,
    "cost_per_unique_panel_type": 250.0,
    "cost_per_panel_element": 45.0,
    "target_surface_types": ["WallSurface"],
    # Basement/below-grade wall surfaces are tagged (not dropped) at extraction time
    # specifically so this can toggle them back in -- default matches the old behaviour
    # (basements were never panelizable at all).
    "include_basement": False,
    "color_mode": "type",
    "tolerance": 0.0001,
    "precision": 6,
    "visualize": True,
    # Off by default: a fresh worker pool costs 1.6-2.6s just to spawn (each Windows worker
    # re-imports numpy/shapely/open3d from scratch), which is a net loss for a one-shot CLI
    # run. It's only worth it with a PERSISTENT pool reused across many calls -- measured
    # 3.6-7.6x faster once warm -- which is why the API layer turns this on and the CLI
    # doesn't (see get_worker_pool). Also requires build_meshes=False: an open3d
    # TriangleMesh can't be pickled back across the process boundary either.
    "parallel": False,
    # Layout re-roll. seed=None keeps the plain aligned grid (unchanged behaviour); set a
    # seed to get a different-but-reproducible arrangement, and change it to re-roll.
    "seed": None,
    "origin_jitter": 1.0,   # 0..1, how far the grid origin may slide (x panel size)
    "stagger": 0.0,         # fixed offset of alternate courses (x panel width); 0.5 = running bond
    "stagger_jitter": 0.0,  # 0..1, extra random per-course offset (needs a seed)
    "size_jitter": 0.0,     # 0..0.5, random +/- variation in panel size (needs a seed)
}


def load_panelizer_config(path: Path | str | None = None) -> dict:
    if path is None:
        return dict(DEFAULT_CONFIG)

    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)

    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    return merged


def save_panel_json(panelization: dict, path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = {key: value for key, value in panelization.items() if key != "panel_meshes"}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(serializable, handle, indent=2)
    return path


def _column_label(index: int) -> str:
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _ring_normal(ring: np.ndarray) -> np.ndarray:
    normal = np.zeros(3, dtype=float)
    for index, point in enumerate(ring):
        normal += np.cross(point, ring[(index + 1) % len(ring)])
    norm = np.linalg.norm(normal)
    if norm < 1e-12:
        raise ValueError("Cannot panelize a wall surface with a degenerate outer ring")
    return normal / norm


def _wall_plane_axes(wall_surface: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rings = [np.asarray(ring, dtype=float) for ring in wall_surface["rings"]]
    outer_ring = rings[0]
    origin = outer_ring.mean(axis=0)
    normal = _ring_normal(outer_ring)
    world_up = np.array([0.0, 0.0, 1.0])

    vertical_axis = world_up - np.dot(world_up, normal) * normal
    vertical_norm = np.linalg.norm(vertical_axis)
    if vertical_norm < 1e-10:
        vertical_axis = outer_ring[1] - outer_ring[0]
        vertical_axis = vertical_axis - np.dot(vertical_axis, normal) * normal
        vertical_norm = np.linalg.norm(vertical_axis)
    if vertical_norm < 1e-10:
        raise ValueError("Cannot calculate a stable upward wall axis")

    vertical_axis = vertical_axis / vertical_norm
    if vertical_axis[2] < 0:
        vertical_axis = -vertical_axis

    horizontal_axis = np.cross(vertical_axis, normal)
    horizontal_axis = horizontal_axis / np.linalg.norm(horizontal_axis)
    return origin, normal, horizontal_axis, vertical_axis


def _project_rings(
    wall_surface: dict,
    origin: np.ndarray,
    horizontal_axis: np.ndarray,
    vertical_axis: np.ndarray,
) -> list[np.ndarray]:
    projected = []
    for ring in wall_surface["rings"]:
        points = np.asarray(ring, dtype=float)
        projected.append(np.stack([
            (points - origin) @ horizontal_axis,
            (points - origin) @ vertical_axis,
        ], axis=1))
    return projected


def _valid_polygon(geometry):
    if geometry.is_empty:
        return geometry
    if not geometry.is_valid:
        geometry = geometry.buffer(0)
    return geometry


def _wall_clip_data(projected_rings: list[np.ndarray]) -> dict:
    outer = [tuple(point) for point in projected_rings[0]]
    outer_polygon = _valid_polygon(Polygon(outer))
    opening_polygons = [
        _valid_polygon(Polygon([tuple(point) for point in ring]))
        for ring in projected_rings[1:]
    ]
    opening_polygons = [
        opening
        for opening in opening_polygons
        if not opening.is_empty and opening.area > 0
    ]
    opening_union = unary_union(opening_polygons) if opening_polygons else GeometryCollection()
    wall_polygon = _valid_polygon(outer_polygon.difference(opening_union))
    if wall_polygon.is_empty:
        raise ValueError("Wall polygon is empty after processing openings")
    return {
        "outer_polygon": outer_polygon,
        "opening_polygons": opening_polygons,
        "opening_union": opening_union,
        "wall_polygon": wall_polygon,
    }


def _edges(min_value: float, max_value: float, target_size: float, tolerance: float,
           offset: float = 0.0) -> list[float]:
    """Grid lines spanning [min_value, max_value] at `target_size` intervals.

    `offset` slides the grid so the first course is a partial panel instead of a full one.
    That shift is what makes one seeded layout read differently from another -- with
    offset 0 this reproduces the original aligned grid exactly.
    """
    if target_size <= 0:
        raise ValueError("Panel width and height must be positive")

    edges = [float(min_value)]
    current = float(min_value) - (float(offset) % target_size)
    while current < max_value - tolerance:
        if current > min_value + tolerance:
            edges.append(float(current))
        current += target_size
    if abs(edges[-1] - max_value) > tolerance:
        edges.append(float(max_value))
    return edges


def _candidate_panel(col: int, row: int, u_edges: list[float], v_edges: list[float]) -> dict:
    u0, u1 = u_edges[col], u_edges[col + 1]
    v0, v1 = v_edges[row], v_edges[row + 1]
    return {
        "name": f"{_column_label(col)}{row}",
        "col": col,
        "row": row,
        "u0": u0,
        "u1": u1,
        "v0": v0,
        "v1": v1,
        "rectangle": box(u0, v0, u1, v1),
        "corners_uv": np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]], dtype=float),
    }


def _candidate_touches_wall(candidate: dict, wall_polygon: Polygon, tolerance: float) -> bool:
    rectangle = candidate["rectangle"]
    if rectangle.intersection(wall_polygon).area > tolerance:
        return True
    return any(
        wall_polygon.covers(Point(float(point[0]), float(point[1])))
        for point in candidate["corners_uv"]
    )


def _clip_panel_to_outer_boundary(candidate: dict, outer_polygon: Polygon, tolerance: float):
    clipped = _valid_polygon(candidate["rectangle"].intersection(outer_polygon))
    if clipped.is_empty or clipped.area <= tolerance:
        return None
    # Intersection area can only shrink or preserve the rectangle's own area -- so it equals
    # the rectangle's area exactly (within tolerance) iff the rectangle sits fully inside
    # outer_polygon, i.e. wasn't clipped. That makes area comparison an exact stand-in for
    # "did clipping actually change the shape", cheaper than the boolean ops call sites used
    # to reach for (symmetric_difference / equals_exact) purely to answer that yes/no.
    was_clipped = clipped.area < candidate["rectangle"].area - tolerance
    return clipped, was_clipped


def _subtract_openings_from_panel(clipped_to_outer, opening_union, tolerance: float):
    if opening_union.is_empty:
        return clipped_to_outer, False
    clipped = _valid_polygon(clipped_to_outer.difference(opening_union))
    if clipped.is_empty or clipped.area <= tolerance:
        return None, False
    was_clipped = clipped.area < clipped_to_outer.area - tolerance
    return clipped, was_clipped


def _clip_panel_to_wall(candidate: dict, wall_clip_data: dict, tolerance: float):
    outer_result = _clip_panel_to_outer_boundary(
        candidate,
        wall_clip_data["outer_polygon"],
        tolerance,
    )
    if outer_result is None:
        return None
    clipped_to_outer, boundary_clipped = outer_result

    clipped, opening_clipped = _subtract_openings_from_panel(
        clipped_to_outer,
        wall_clip_data["opening_union"],
        tolerance,
    )
    if clipped is None:
        return None
    return clipped, boundary_clipped or opening_clipped


def _iter_polygons(geometry, tolerance: float = 0.0):
    if isinstance(geometry, Polygon):
        if not geometry.is_empty and geometry.area > tolerance:
            yield geometry
    elif isinstance(geometry, MultiPolygon):
        for polygon in geometry.geoms:
            if not polygon.is_empty and polygon.area > tolerance:
                yield polygon
    elif isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from _iter_polygons(item, tolerance)


def _normalize_panel_piece(polygon: Polygon, tolerance: float, already_valid: bool = False) -> Polygon | None:
    if not already_valid:
        polygon = _valid_polygon(polygon)
    if polygon.is_empty or polygon.area <= tolerance:
        return None
    if not isinstance(polygon, Polygon):
        pieces = list(_iter_polygons(polygon, tolerance))
        if not pieces:
            return None
        polygon = max(pieces, key=lambda item: item.area)
    return polygon


def _normalize_panel_geometry(geometry, tolerance: float, already_valid: bool = False) -> list[Polygon]:
    """`already_valid=True` skips re-running shapely's `is_valid` check (and the `buffer(0)`
    repair it can trigger) when the caller already validated `geometry` immediately before
    calling this -- e.g. `_clip_panel_to_wall`'s result. GEOS overlay ops (intersection/
    difference) don't introduce new invalidity beyond what that one check already covers,
    and validity is inherited by every sub-geometry `.geoms` yields, so re-checking a
    just-validated object (and every polygon extracted from it) is pure repeated work --
    this was previously happening up to 3x for the same geometry on every panel.
    """
    source = geometry if already_valid else _valid_polygon(geometry)
    pieces = []
    for polygon in _iter_polygons(source, tolerance):
        normalized = _normalize_panel_piece(polygon, tolerance, already_valid=already_valid)
        if normalized is not None:
            pieces.append(normalized)
    return sorted(pieces, key=lambda item: (item.bounds[0], item.bounds[1], -item.area))


def _polygon_to_uv_rings(polygon: Polygon, precision: int) -> list[list[list[float]]]:
    rings = []
    exterior = list(polygon.exterior.coords)[:-1]
    rings.append([[round(float(x), precision), round(float(y), precision)] for x, y in exterior])
    for interior in polygon.interiors:
        coords = list(interior.coords)[:-1]
        rings.append([[round(float(x), precision), round(float(y), precision)] for x, y in coords])
    return rings


def _uv_to_xyz(point: tuple[float, float], origin: np.ndarray, horizontal_axis: np.ndarray, vertical_axis: np.ndarray) -> np.ndarray:
    return origin + float(point[0]) * horizontal_axis + float(point[1]) * vertical_axis


def _polygon_to_xyz_rings(
    polygon: Polygon,
    origin: np.ndarray,
    horizontal_axis: np.ndarray,
    vertical_axis: np.ndarray,
    precision: int,
) -> list[list[list[float]]]:
    xyz_rings = []
    for ring in _polygon_to_uv_rings(polygon, precision=precision):
        xyz_rings.append([
            np.round(_uv_to_xyz(point, origin, horizontal_axis, vertical_axis), precision).tolist()
            for point in ring
        ])
    return xyz_rings


def _panel_color(panel: dict) -> list[float]:
    if panel["is_specialized"]:
        return [0.95, 0.45, 0.15]
    if panel["is_residual_width"]:
        return [0.90, 0.75, 0.20]
    if panel["is_residual_height"]:
        return [0.35, 0.65, 0.95]
    return [0.25, 0.70, 0.45]


def _mesh_from_panel_polygons(
    polygons: list[Polygon],
    origin: np.ndarray,
    horizontal_axis: np.ndarray,
    vertical_axis: np.ndarray,
    color: list[float],
):
    vertices = []
    triangles = []
    vertex_index = {}

    def add_point(point: tuple[float, float]) -> int:
        key = (round(float(point[0]), 9), round(float(point[1]), 9))
        if key not in vertex_index:
            vertex_index[key] = len(vertices)
            vertices.append(_uv_to_xyz(key, origin, horizontal_axis, vertical_axis))
        return vertex_index[key]

    def add_triangle_coords(coords):
        triangles.append([add_point(point) for point in coords])

    for polygon in polygons:
        if earcut is not None:
            rings = [list(polygon.exterior.coords)[:-1]]
            rings.extend(list(interior.coords)[:-1] for interior in polygon.interiors)
            vertices_2d = np.asarray([point for ring in rings for point in ring], dtype=np.float64)
            ring_ends = np.cumsum([len(ring) for ring in rings]).astype(np.uint32)
            panel_triangles = earcut.triangulate_float64(vertices_2d, ring_ends).reshape((-1, 3))
            for triangle in panel_triangles:
                add_triangle_coords([tuple(vertices_2d[index]) for index in triangle])
            continue

        for triangle in triangulate(polygon):
            if triangle.intersection(polygon).area < triangle.area - 1e-8:
                continue
            coords = list(triangle.exterior.coords)[:-1]
            if len(coords) != 3:
                continue
            add_triangle_coords(coords)

    mesh = o3d.geometry.TriangleMesh()
    mesh.vertices = o3d.utility.Vector3dVector(np.asarray(vertices, dtype=float))
    mesh.triangles = o3d.utility.Vector3iVector(np.asarray(triangles, dtype=int))
    mesh.paint_uniform_color(color)
    if triangles:
        mesh.compute_vertex_normals()
    return mesh


def _round(value: float, precision: int) -> float:
    return round(float(value), precision)


def _panel_dimensions(candidate: dict, clipped_geometry, was_clipped: bool) -> tuple[float, float]:
    if not was_clipped:
        return candidate["u1"] - candidate["u0"], candidate["v1"] - candidate["v0"]
    min_u, min_v, max_u, max_v = clipped_geometry.bounds
    return max_u - min_u, max_v - min_v


def _build_panel_record(
    candidate: dict,
    clipped_geometry,
    was_clipped: bool,
    panel_width: float,
    panel_height: float,
    origin: np.ndarray,
    horizontal_axis: np.ndarray,
    vertical_axis: np.ndarray,
    tolerance: float,
    precision: int,
) -> tuple[dict, list[Polygon]]:
    # clipped_geometry was already validated by _clip_panel_to_wall right before this call
    polygons = _normalize_panel_geometry(clipped_geometry, tolerance, already_valid=True)
    width, height = _panel_dimensions(candidate, clipped_geometry, was_clipped)
    width = _round(width, precision)
    height = _round(height, precision)
    is_residual_width = abs((candidate["u1"] - candidate["u0"]) - panel_width) > tolerance
    is_residual_height = abs((candidate["v1"] - candidate["v0"]) - panel_height) > tolerance
    is_clipped = was_clipped

    panel = {
        "name": candidate["name"],
        "col": candidate["col"],
        "row": candidate["row"],
        "width": width,
        "height": height,
        "area": _round(clipped_geometry.area, precision),
        "is_unique": is_residual_width or is_residual_height or is_clipped,
        "is_residual_width": is_residual_width,
        "is_residual_height": is_residual_height,
        "is_specialized": is_clipped,
        "n_vertices": sum(len(list(polygon.exterior.coords)) - 1 for polygon in polygons),
        "n_holes": sum(len(polygon.interiors) for polygon in polygons),
        "n_pieces": len(polygons),
        "polygons_uv": [_polygon_to_uv_rings(polygon, precision) for polygon in polygons],
        "polygons_xyz": [
            _polygon_to_xyz_rings(polygon, origin, horizontal_axis, vertical_axis, precision)
            for polygon in polygons
        ],
    }

    if len(polygons) == 1 and not polygons[0].interiors:
        panel["corners_xyz"] = panel["polygons_xyz"][0][0]

    return panel, polygons


def wall_layout(seed, wall_id: int, panel_width: float, panel_height: float, config: dict) -> dict:
    """Per-wall grid placement. With no seed this is the plain aligned grid; with one it's a
    reproducible re-roll -- the grid origin slides, courses stagger, panel size can vary.

    The RNG is derived from (seed, wall_id) rather than consumed from a single shared stream,
    so one wall's result doesn't shift when another wall's geometry changes.
    """
    if seed is None:
        return {"u_offset": 0.0, "v_offset": 0.0, "stagger": float(config.get("stagger", 0.0)),
                "stagger_jitter": 0.0, "width": panel_width, "height": panel_height, "rng": None}

    rng = np.random.default_rng([int(seed), int(wall_id)])
    origin_jitter = float(config.get("origin_jitter", 1.0))
    size_jitter = float(config.get("size_jitter", 0.0))

    width = panel_width * (1.0 + rng.uniform(-size_jitter, size_jitter)) if size_jitter else panel_width
    height = panel_height * (1.0 + rng.uniform(-size_jitter, size_jitter)) if size_jitter else panel_height

    return {
        "u_offset": rng.uniform(0.0, width) * origin_jitter,
        "v_offset": rng.uniform(0.0, height) * origin_jitter,
        "stagger": float(config.get("stagger", 0.0)),
        "stagger_jitter": float(config.get("stagger_jitter", 0.0)),
        "width": width,
        "height": height,
        "rng": rng,
    }


def panelize_wall_surface(
    wall_surface: dict,
    wall_id: int,
    panel_width: float,
    panel_height: float,
    tolerance: float = 0.0001,
    precision: int = 6,
    layout: dict | None = None,
    build_meshes: bool = True,
) -> tuple[dict, list]:
    layout = layout or {"u_offset": 0.0, "v_offset": 0.0, "stagger": 0.0,
                        "stagger_jitter": 0.0, "width": panel_width, "height": panel_height, "rng": None}
    panel_width = layout["width"]
    panel_height = layout["height"]

    origin, normal, horizontal_axis, vertical_axis = _wall_plane_axes(wall_surface)
    projected_rings = _project_rings(wall_surface, origin, horizontal_axis, vertical_axis)
    wall_clip_data = _wall_clip_data(projected_rings)
    wall_polygon = wall_clip_data["wall_polygon"]
    min_u, min_v, max_u, max_v = wall_polygon.bounds
    v_edges = _edges(min_v, max_v, panel_height, tolerance, layout["v_offset"])

    panels = []
    panel_meshes = []
    skipped = 0
    max_cols = 0

    # u_edges are rebuilt per course so rows can stagger against each other; with no stagger
    # and no jitter every row gets the same edges, i.e. the original aligned grid
    for row in range(len(v_edges) - 1):
        u_offset = layout["u_offset"] + layout["stagger"] * panel_width * (row % 2)
        if layout["rng"] is not None and layout["stagger_jitter"]:
            u_offset += layout["rng"].uniform(0.0, panel_width) * layout["stagger_jitter"]
        u_edges = _edges(min_u, max_u, panel_width, tolerance, u_offset)
        max_cols = max(max_cols, len(u_edges) - 1)

        for col in range(len(u_edges) - 1):
            candidate = _candidate_panel(col, row, u_edges, v_edges)
            if not _candidate_touches_wall(candidate, wall_polygon, tolerance):
                skipped += 1
                continue

            clip_result = _clip_panel_to_wall(candidate, wall_clip_data, tolerance)
            if clip_result is None:
                skipped += 1
                continue
            clipped, was_clipped = clip_result

            panel, polygons = _build_panel_record(
                candidate,
                clipped,
                was_clipped,
                panel_width,
                panel_height,
                origin,
                horizontal_axis,
                vertical_axis,
                tolerance,
                precision,
            )
            panels.append(panel)
            # panel_meshes (open3d TriangleMesh, one per panel) is only ever consumed by
            # visualize_panels() for the local desktop viewer -- panelization_to_viewer_payload
            # never sends it to the API/UI, and save_panel_json strips it before writing to
            # disk. Building it anyway when nothing will use it costs a measured 22-25% of
            # panelize time (0.4-0.7s), so it's skipped by default; build_meshes is threaded
            # from config["visualize"], so `--visualize`/an explicit visualize:true still works.
            if build_meshes:
                panel_meshes.append(
                    _mesh_from_panel_polygons(
                        polygons,
                        origin,
                        horizontal_axis,
                        vertical_axis,
                        _panel_color(panel),
                    )
                )

    unique_types = {
        (panel["width"], panel["height"], panel["n_vertices"], panel["n_pieces"])
        for panel in panels
        if panel["is_unique"]
    }
    wall = {
        "wall_id": wall_id,
        "wall_type": wall_surface.get("semantic_type", "WallSurface"),
        "width": _round(max_u - min_u, precision),
        "height": _round(max_v - min_v, precision),
        "area": _round(wall_polygon.area, precision),
        "n_openings": max(0, len(projected_rings) - 1),
        "n_cols": max_cols,
        "n_rows": len(v_edges) - 1,
        "n_panels": len(panels),
        "n_unique_panels": sum(1 for panel in panels if panel["is_unique"]),
        "n_specialized_panels": sum(1 for panel in panels if panel["is_specialized"]),
        "n_unique_types": len(unique_types),
        "normal": np.round(normal, precision).tolist(),
        "panels": panels,
    }
    return wall, panel_meshes


def _wall_surfaces(building: dict, include_basement: bool = False) -> list[dict]:
    if "surfaces" in building and "wall" in building["surfaces"]:
        walls = building["surfaces"]["wall"]
    else:
        walls = [
            {"mesh": mesh, "rings": [np.asarray(mesh.vertices, dtype=float).tolist()], "semantic_type": "WallSurface"}
            for mesh in building["meshes"]["wall"]
        ]
    if include_basement:
        return walls
    return [w for w in walls if not w.get("is_basement")]


_WORKER_POOL: ProcessPoolExecutor | None = None
_WORKER_POOL_LOCK = threading.Lock()


def get_worker_pool() -> ProcessPoolExecutor:
    """A process pool created once and reused for the life of this process.

    Deliberately a module-level singleton, not created fresh per call: see the note on
    DEFAULT_CONFIG["parallel"] for why a persistent pool is the whole point. Thread-safe
    (double-checked locking) because FastAPI runs sync endpoints in a thread pool, so two
    concurrent requests could race to create this on first use.
    """
    global _WORKER_POOL
    if _WORKER_POOL is None:
        with _WORKER_POOL_LOCK:
            if _WORKER_POOL is None:
                _WORKER_POOL = ProcessPoolExecutor(max_workers=os.cpu_count())
    return _WORKER_POOL


def shutdown_worker_pool():
    """Release worker processes. Call this on app/process shutdown (e.g. a FastAPI
    shutdown event) so they don't linger after the parent exits."""
    global _WORKER_POOL
    if _WORKER_POOL is not None:
        _WORKER_POOL.shutdown(wait=False, cancel_futures=True)
        _WORKER_POOL = None


def _strip_unpicklable_wall_surface(wall_surface: dict) -> dict:
    """wall_surface carries an open3d.TriangleMesh under "mesh" (built for the whole-
    building 3D viewer) that panelize_wall_surface never reads -- only "rings",
    "semantic_type" and "is_basement" matter here. open3d meshes can't cross a process
    boundary at all (confirmed: pickling one raises TypeError), so this has to come off
    before a wall_surface is handed to a worker."""
    return {key: value for key, value in wall_surface.items() if key != "mesh"}


def _panelize_wall_task(args: tuple) -> tuple[dict, list]:
    """Top-level (picklable) entry point run in a worker process. build_meshes is always
    False here: an open3d TriangleMesh can't be pickled back across the process boundary
    either, so a parallel run and --visualize are mutually exclusive by construction (see
    panelize_buildings, which only takes this path when build_meshes is already False)."""
    wall_surface, wall_id, panel_width, panel_height, tolerance, precision, layout = args
    return panelize_wall_surface(
        wall_surface, wall_id, panel_width, panel_height, tolerance, precision, layout, build_meshes=False,
    )


def panelize_buildings(
    buildings: list[dict],
    config: dict | None = None,
    output_json_path: Path | str | None = None,
) -> dict:
    config = dict(DEFAULT_CONFIG if config is None else {**DEFAULT_CONFIG, **config})
    panel_width = float(config["panel_width"])
    panel_height = float(config["panel_height"])
    cost_per_unique_panel_type = float(config["cost_per_unique_panel_type"])
    cost_per_panel_element = float(config["cost_per_panel_element"])
    tolerance = float(config["tolerance"])
    precision = int(config["precision"])
    seed = config.get("seed")
    seed = None if seed is None or seed == "" else int(seed)
    include_basement = bool(config.get("include_basement", False))
    # panel_meshes are only ever consumed by visualize_panels() (the local desktop viewer),
    # gated behind this same flag -- skip building them entirely otherwise (see the note in
    # panelize_wall_surface for why that's worth doing).
    build_meshes = bool(config.get("visualize", False))
    # Parallel execution and mesh-building are mutually exclusive: an open3d TriangleMesh
    # can't cross the process boundary (see _panelize_wall_task), so this only applies when
    # nothing needs meshes anyway -- which is already the common case (build_meshes is off
    # by default; see its own note above).
    use_parallel = bool(config.get("parallel", False)) and not build_meshes

    parts = []
    all_panel_meshes = []
    total_panels = 0
    total_unique_panels = 0
    total_specialized_panels = 0
    total_unique_types = set()
    total_walls = 0

    for building in buildings:
        wall_entries = []
        part_panel_meshes = []
        walls = list(enumerate(_wall_surfaces(building, include_basement), start=1))

        if use_parallel and len(walls) > 1:
            # ProcessPoolExecutor.map preserves input order in its output, so wall_entries
            # ends up identical to the sequential loop below regardless of which worker
            # actually finished a given wall first.
            tasks = [
                (
                    _strip_unpicklable_wall_surface(wall_surface),
                    wall_index,
                    panel_width,
                    panel_height,
                    tolerance,
                    precision,
                    wall_layout(seed, wall_index, panel_width, panel_height, config),
                )
                for wall_index, wall_surface in walls
            ]
            workers = os.cpu_count() or 1
            chunksize = max(1, len(tasks) // (workers * 4))
            for wall, wall_panel_meshes in get_worker_pool().map(_panelize_wall_task, tasks, chunksize=chunksize):
                wall_entries.append(wall)
                part_panel_meshes.extend(wall_panel_meshes)
        else:
            for wall_index, wall_surface in walls:
                wall, wall_panel_meshes = panelize_wall_surface(
                    wall_surface,
                    wall_id=wall_index,
                    panel_width=panel_width,
                    panel_height=panel_height,
                    tolerance=tolerance,
                    precision=precision,
                    layout=wall_layout(seed, wall_index, panel_width, panel_height, config),
                    build_meshes=build_meshes,
                )
                wall_entries.append(wall)
                part_panel_meshes.extend(wall_panel_meshes)

        part_total_panels = sum(wall["n_panels"] for wall in wall_entries)
        part_total_unique_panels = sum(wall["n_unique_panels"] for wall in wall_entries)
        part_total_specialized_panels = sum(wall["n_specialized_panels"] for wall in wall_entries)
        part_unique_types = {
            (panel["width"], panel["height"], panel["n_vertices"], panel["n_pieces"])
            for wall in wall_entries
            for panel in wall["panels"]
            if panel["is_unique"]
        }
        # Single source of truth for the building-level aggregate too -- this already
        # iterates every wall in wall_entries regardless of which branch (parallel or
        # sequential) populated it, so it can't drift between the two the way a separate
        # per-wall accumulator inside just one branch did.
        total_unique_types.update(part_unique_types)

        parts.append({
            "building_id": building["id"],
            "parent_id": str(building["id"]).split("-")[0],
            "total_panels": part_total_panels,
            "total_unique_panels": part_total_unique_panels,
            "total_specialized_panels": part_total_specialized_panels,
            "total_unique_types": len(part_unique_types),
            "walls": wall_entries,
        })
        all_panel_meshes.extend(part_panel_meshes)
        total_panels += part_total_panels
        total_unique_panels += part_total_unique_panels
        total_specialized_panels += part_total_specialized_panels
        total_walls += len(wall_entries)

    parent_id = str(parts[0]["parent_id"]) if parts else ""
    panelization = {
        "building_id": parent_id,
        "config": {
            "panel_width": panel_width,
            "panel_height": panel_height,
            "cost_per_unique_panel_type": cost_per_unique_panel_type,
            "cost_per_panel_element": cost_per_panel_element,
            "target_surface_types": config["target_surface_types"],
            "include_basement": include_basement,
            "color_mode": config["color_mode"],
        },
        "summary": {
            "n_parts": len(parts),
            "n_walls": total_walls,
            "total_panels": total_panels,
            "total_unique_panels": total_unique_panels,
            "total_specialized_panels": total_specialized_panels,
            "total_unique_types": len(total_unique_types),
            "cost_total": round(
                len(total_unique_types) * cost_per_unique_panel_type
                + total_panels * cost_per_panel_element,
                2,
            ),
            "cost_unique_panel_types": round(len(total_unique_types) * cost_per_unique_panel_type, 2),
            "cost_panel_elements": round(total_panels * cost_per_panel_element, 2),
        },
        "parts": parts,
        "panel_meshes": all_panel_meshes,
    }

    if output_json_path is not None:
        save_panel_json(panelization, output_json_path)

    return panelization


def panelize_building_walls(
    building: dict,
    config: dict | None = None,
    output_json_path: Path | str | None = None,
) -> dict:
    return panelize_buildings([building], config=config, output_json_path=output_json_path)


def visualize_panels(panelization: dict):
    meshes = list(panelization.get("panel_meshes", []))
    if not meshes:
        print("No panel meshes to visualize")
        return []
    o3d.visualization.draw_geometries(meshes)
    return meshes
