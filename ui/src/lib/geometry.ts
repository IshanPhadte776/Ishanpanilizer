import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { Ring3, Vec3 } from "../types";

export function centerOfRings(rings: Ring3[]): THREE.Vector3 {
  const center = new THREE.Vector3();
  let count = 0;
  rings.forEach((ring) => {
    ring.forEach((point) => {
      center.add(toVector(point));
      count += 1;
    });
  });
  return count ? center.divideScalar(count) : center;
}

export function sceneCenter(rings: Ring3[]): THREE.Vector3 {
  return centerOfRings(rings);
}

export function sceneGroundOffset(rings: Ring3[]): THREE.Vector3 {
  const center = centerOfRings(rings);
  let minZ = Number.POSITIVE_INFINITY;
  rings.forEach((ring) => {
    ring.forEach((point) => {
      minZ = Math.min(minZ, point[2]);
    });
  });
  return new THREE.Vector3(center.x, center.y, Number.isFinite(minZ) ? minZ : center.z);
}

export function polygonGeometry(rings: Ring3[], offset: THREE.Vector3): THREE.BufferGeometry {
  const exterior = rings[0];
  const vertices: number[] = [];
  const points3 = rings.flatMap((ring) => ring.map(toVector));
  points3.forEach((point) => {
    const v = point.clone().sub(offset);
    vertices.push(v.x, v.y, v.z);
  });

  const { origin, axisU, axisV } = polygonBasis(exterior);
  const points2 = points3.map((point) => {
    const relative = point.clone().sub(origin);
    return new THREE.Vector2(relative.dot(axisU), relative.dot(axisV));
  });

  const contour = points2.slice(0, exterior.length);
  const holes: THREE.Vector2[][] = [];
  let cursor = exterior.length;
  for (const ring of rings.slice(1)) {
    holes.push(points2.slice(cursor, cursor + ring.length));
    cursor += ring.length;
  }

  const triangles = THREE.ShapeUtils.triangulateShape(contour, holes);
  const indices = triangles.flat();

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}

export function exteriorFanGeometry(ring: Ring3, offset: THREE.Vector3): THREE.BufferGeometry {
  const vertices: number[] = [];
  const indices: number[] = [];
  ring.forEach((point) => {
    const v = toVector(point).sub(offset);
    vertices.push(v.x, v.y, v.z);
  });
  for (let index = 1; index < ring.length - 1; index += 1) {
    indices.push(0, index, index + 1);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}

export type MergedColorGroup = {
  geometry: THREE.BufferGeometry | null;
  /** Outline of each polygon's own boundary -- drawn whenever `alwaysOutlineExterior` is
   *  true (panels), or the polygon has holes (a wall's own opening cutouts otherwise read
   *  as an unbroken fill with no edge at all). */
  exteriorOutline: THREE.BufferGeometry | null;
  /** Hole-ring outlines (a wall's window/door cutouts) -- always drawn when present,
   *  slightly more opaque than the exterior outline so openings read clearly against the
   *  wall fill regardless of `alwaysOutlineExterior`. */
  holeOutline: THREE.BufferGeometry | null;
};

/** Merge every item sharing one material into a single mesh geometry and up to two outline
 *  geometries (exterior vs holes), instead of one mesh + one-or-two line loops per item.
 *
 *  A BIM-derived scene is thousands of small polygons -- each is its own three.js
 *  mesh/line object with its own draw call. Merging changes nothing about what's drawn
 *  (same triangles, same edges, same material per merged group), only how many times the
 *  GPU is asked to draw it: outline edges become independent segment pairs in one
 *  `LineSegments` buffer per group, since these polygons don't share edges/connectivity
 *  the way a `LineLoop` needs, but disconnected segment pairs merge freely.
 */
export function mergePolygonGroup(
  items: { rings: Ring3[] }[],
  offset: THREE.Vector3,
  alwaysOutlineExterior: boolean,
): MergedColorGroup {
  const meshGeometries: THREE.BufferGeometry[] = [];
  const exteriorPositions: number[] = [];
  const holePositions: number[] = [];

  const pushSegments = (target: number[], ring: Ring3) => {
    for (let i = 0; i < ring.length; i += 1) {
      const a = ring[i];
      const b = ring[(i + 1) % ring.length];
      target.push(a[0] - offset.x, a[1] - offset.y, a[2] - offset.z, b[0] - offset.x, b[1] - offset.y, b[2] - offset.z);
    }
  };

  for (const item of items) {
    const exterior = item.rings[0];
    if (!exterior || exterior.length < 3) continue;
    meshGeometries.push(polygonGeometry(item.rings, offset));

    const hasHoles = item.rings.length > 1;
    if (alwaysOutlineExterior || hasHoles) pushSegments(exteriorPositions, exterior);
    for (const hole of item.rings.slice(1)) pushSegments(holePositions, hole);
  }

  const geometry = meshGeometries.length ? mergeGeometries(meshGeometries, false) : null;
  meshGeometries.forEach((piece) => piece.dispose()); // merged into `geometry`; originals unneeded

  const toLineGeometry = (positions: number[]): THREE.BufferGeometry | null => {
    if (positions.length === 0) return null;
    const lineGeom = new THREE.BufferGeometry();
    lineGeom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    return lineGeom;
  };

  return {
    geometry,
    exteriorOutline: toLineGeometry(exteriorPositions),
    holeOutline: toLineGeometry(holePositions),
  };
}

export function collectRingsFromBuilding(parts: { surfaces: { rings: Ring3[] }[] }[]): Ring3[] {
  return parts.flatMap((part) => part.surfaces.flatMap((surface) => surface.rings));
}

export function collectRingsFromPanels(parts: { walls: { panels: { polygons_xyz: Ring3[][] }[] }[] }[]): Ring3[] {
  return parts.flatMap((part) =>
    part.walls.flatMap((wall) =>
      wall.panels.flatMap((panel) => panel.polygons_xyz.flatMap((piece) => piece)),
    ),
  );
}

function toVector(point: Vec3): THREE.Vector3 {
  return new THREE.Vector3(point[0], point[1], point[2]);
}

function polygonBasis(ring: Ring3) {
  const origin = centerOfRings([ring]);
  const normal = new THREE.Vector3();
  const points = ring.map(toVector);

  points.forEach((point, index) => {
    const next = points[(index + 1) % points.length];
    normal.x += (point.y - next.y) * (point.z + next.z);
    normal.y += (point.z - next.z) * (point.x + next.x);
    normal.z += (point.x - next.x) * (point.y + next.y);
  });
  normal.normalize();

  let axisU = new THREE.Vector3(1, 0, 0);
  let longest = 0;
  points.forEach((point, index) => {
    const edge = points[(index + 1) % points.length].clone().sub(point);
    const length = edge.length();
    if (length > longest) {
      longest = length;
      axisU = edge;
    }
  });
  axisU.addScaledVector(normal, -axisU.dot(normal)).normalize();
  const axisV = new THREE.Vector3().crossVectors(normal, axisU).normalize();
  return { origin, axisU, axisV };
}
