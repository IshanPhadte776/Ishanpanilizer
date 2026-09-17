import { OrbitControls } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { Info, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import * as THREE from "three";
import type { BuildingPayload, Layers, Panel, PanelizationPayload, Ring3 } from "../types";
import {
  collectRingsFromBuilding,
  collectRingsFromPanels,
  mergePolygonGroup,
  sceneGroundOffset,
  type MergedColorGroup,
} from "../lib/geometry";

type ViewerProps = {
  building?: BuildingPayload;
  panelization?: PanelizationPayload;
  layers: Layers;
  includeBasement: boolean;
};

const surfaceColors: Record<string, string> = {
  roof: "#b9473d",
  wall: "#c7ac86",
  reveal: "#8b735a",
  balcony: "#8b55a1",
  other: "#6f94bc",
};

// Single source of truth for panel colors -- reused by both the mesh coloring below and
// the Legend, so the key can never silently drift out of sync with what's drawn.
const panelColors = {
  specialized: "#f06f2f",
  unique: "#e0bd3d",
  standard: "#39a96b",
};

function panelColor(panel: Panel): string {
  if (panel.is_specialized) return panelColors.specialized;
  if (panel.is_unique) return panelColors.unique;
  return panelColors.standard;
}

export function Viewer({ building, panelization, layers, includeBasement }: ViewerProps) {
  // Stable across re-renders that don't actually change the geometry, so it can safely be
  // a useMemo dependency below -- a fresh Vector3 every render would invalidate the merge
  // memo every time regardless of whether anything actually changed.
  const sceneOffset = useMemo(() => {
    const buildingRings = building ? collectRingsFromBuilding(building.parts) : [];
    const panelRings = panelization ? collectRingsFromPanels(panelization.parts) : [];
    return sceneGroundOffset([...buildingRings, ...panelRings]);
  }, [building, panelization]);

  // One merged mesh + up to two merged outline buffers PER COLOR, instead of one mesh +
  // outline per surface -- a BIM building is thousands of small surfaces, and each used to
  // be its own draw call (and its own re-triangulation on every render, since neither was
  // memoized before). Recomputes only when the actual inputs change.
  const buildingGroups = useMemo(() => {
    const byColor = new Map<string, { rings: Ring3[] }[]>();
    building?.parts.forEach((part) => {
      part.surfaces.forEach((surface) => {
        if (!layers[surface.category as keyof Layers]) return;
        if (surface.is_basement && !includeBasement) return;
        const color = surfaceColors[surface.category] ?? surfaceColors.other;
        const list = byColor.get(color) ?? [];
        list.push({ rings: surface.rings });
        byColor.set(color, list);
      });
    });
    const merged = new Map<string, MergedColorGroup>();
    byColor.forEach((items, color) => merged.set(color, mergePolygonGroup(items, sceneOffset, false)));
    return merged;
  }, [building, layers, includeBasement, sceneOffset]);

  const panelGroups = useMemo(() => {
    const byColor = new Map<string, { rings: Ring3[] }[]>();
    if (layers.panels) {
      panelization?.parts.forEach((part) => {
        part.walls.forEach((wall) => {
          wall.panels.forEach((panel) => {
            if (panel.is_specialized && !layers.specialized) return;
            const color = panelColor(panel);
            const list = byColor.get(color) ?? [];
            panel.polygons_xyz.forEach((piece) => list.push({ rings: piece }));
            byColor.set(color, list);
          });
        });
      });
    }
    const merged = new Map<string, MergedColorGroup>();
    byColor.forEach((items, color) => merged.set(color, mergePolygonGroup(items, sceneOffset, true)));
    return merged;
  }, [panelization, layers, sceneOffset]);

  // GPU buffers aren't freed by JS garbage collection -- dispose the previous merge's
  // geometries whenever a new one replaces it (dependency change) or the viewer unmounts.
  useEffect(() => () => disposeGroups(buildingGroups), [buildingGroups]);
  useEffect(() => () => disposeGroups(panelGroups), [panelGroups]);

  // Draw calls/triangles are what the merge above actually changes -- the server-side
  // model-load/panelize timers in the sidebar measure Python work that finishes before any
  // of this renders, so they can't show it. This reads the real number straight from the
  // WebGL renderer, written directly to the DOM (not React state) so a 60x/sec update
  // doesn't itself become a performance problem.
  const statsRef = useRef<HTMLDivElement>(null);

  return (
    <>
      <Canvas camera={{ position: [0, -28, 18], fov: 45 }} dpr={[1, 1.5]}>
        <color attach="background" args={["#f5f7f9"]} />
        <ambientLight intensity={0.75} />
        <directionalLight position={[20, -30, 40]} intensity={1.2} />
        <group rotation={[-Math.PI / 2, 0, 0]}>
          <ColorGroupMeshes prefix="building" groups={buildingGroups} opacity={0.48} />
          <ColorGroupMeshes prefix="panel" groups={panelGroups} opacity={0.86} />
        </group>
        <gridHelper args={[40, 40, "#9aa5b1", "#d4dae2"]} />
        <OrbitControls makeDefault enableDamping />
        <RenderStatsReader targetRef={statsRef} />
      </Canvas>
      <Legend layers={layers} />
      <div className="renderStats" ref={statsRef} />
    </>
  );
}

function RenderStatsReader({ targetRef }: { targetRef: React.RefObject<HTMLDivElement> }) {
  const frame = useRef(0);
  useFrame(({ gl }) => {
    frame.current += 1;
    if (frame.current % 15 !== 0) return; // a few updates/sec is plenty for a readout
    const target = targetRef.current;
    if (!target) return;
    const { calls, triangles } = gl.info.render;
    target.textContent = `${calls} draw call${calls === 1 ? "" : "s"} · ${triangles.toLocaleString()} triangles`;
  });
  return null;
}

function disposeGroups(groups: Map<string, MergedColorGroup>) {
  groups.forEach((group) => {
    group.geometry?.dispose();
    group.exteriorOutline?.dispose();
    group.holeOutline?.dispose();
  });
}

function ColorGroupMeshes({
  prefix,
  groups,
  opacity,
}: {
  prefix: string;
  groups: Map<string, MergedColorGroup>;
  opacity: number;
}) {
  return (
    <>
      {Array.from(groups.entries()).map(([color, group]) => (
        <group key={`${prefix}-${color}`}>
          {group.geometry && (
            <mesh geometry={group.geometry}>
              <meshStandardMaterial color={color} side={THREE.DoubleSide} transparent opacity={opacity} />
            </mesh>
          )}
          {group.exteriorOutline && (
            <lineSegments geometry={group.exteriorOutline}>
              <lineBasicMaterial color="#1f2933" transparent opacity={0.55} />
            </lineSegments>
          )}
          {group.holeOutline && (
            <lineSegments geometry={group.holeOutline}>
              <lineBasicMaterial color="#1f2933" transparent opacity={0.75} />
            </lineSegments>
          )}
        </group>
      ))}
    </>
  );
}

function Legend({ layers }: { layers: Layers }) {
  const [showPanelInfo, setShowPanelInfo] = useState(false);
  const visibleSurfaceCategories = (Object.keys(surfaceColors) as (keyof Layers)[]).filter(
    (category) => layers[category],
  );

  return (
    <div className="legend">
      {layers.panels && (
        <div className="legendGroup">
          <div className="legendHeading">
            <h3>Panels</h3>
            <button
              type="button"
              className="legendInfoButton"
              onClick={() => setShowPanelInfo(true)}
              aria-label="Explain panel types"
              title="Explain panel types"
            >
              <Info size={13} />
            </button>
          </div>
          <LegendRow color={panelColors.standard} label="Standard" />
          <LegendRow color={panelColors.unique} label="Unique size" />
          {layers.specialized && <LegendRow color={panelColors.specialized} label="Specialized" />}
        </div>
      )}
      {visibleSurfaceCategories.length > 0 && (
        <div className="legendGroup">
          <h3>Surfaces</h3>
          {visibleSurfaceCategories.map((category) => (
            <LegendRow key={category} color={surfaceColors[category]} label={surfaceLabel(category)} />
          ))}
        </div>
      )}
      {showPanelInfo && <PanelTypesModal onClose={() => setShowPanelInfo(false)} />}
    </div>
  );
}

function PanelTypesModal({ onClose }: { onClose: () => void }) {
  return (
    <div className="modalOverlay" onClick={onClose}>
      <div className="modalCard" onClick={(event) => event.stopPropagation()}>
        <div className="modalHeader">
          <h2>Panel types</h2>
          <button type="button" className="modalCloseButton" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <PanelTypeEntry color={panelColors.standard} title="Standard">
          A full, uncut panel at exactly the configured Panel width × Panel height. This is the
          cheapest, most repeatable case -- it's just the plain grid unit, wherever a full cell
          fits on the wall with nothing in the way.
        </PanelTypeEntry>

        <PanelTypeEntry color={panelColors.unique} title="Unique size">
          Still a plain rectangle, but resized smaller in width and/or height than the standard
          grid unit. This happens wherever the leftover space at the end of a course, or above/
          below an opening, doesn't divide evenly into full panel-sized units -- e.g. a run of
          1.2 m panels with 0.4 m of wall left over gets one 0.4 m-wide panel instead of a
          partial standard one. Each distinct size like this is its own "unique panel type,"
          which is what the per-type tooling cost in the cost estimate charges for.
        </PanelTypeEntry>

        <PanelTypeEntry color={panelColors.specialized} title="Specialized">
          A panel whose outline itself is irregular -- clipped around a window or door opening,
          or cut along an angled/curved section of wall -- rather than a plain rectangle at any
          size. These need custom, non-rectangular fabrication, so they're the most complex
          (and usually most expensive per panel) category, and they almost always count as
          their own unique panel type as well, since no other panel shares that exact shape.
        </PanelTypeEntry>

        <p className="modalFootnote">
          Every panel that isn't a plain full-size rectangle counts as "Unique" in the summary
          bar below -- Specialized panels are a stricter subset of that (an irregular shape,
          not just a resized rectangle), which is why the viewer draws them as their own color
          instead of folding them into "Unique size."
        </p>
      </div>
    </div>
  );
}

function PanelTypeEntry({
  color,
  title,
  children,
}: {
  color: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="panelTypeEntry">
      <div className="panelTypeHeading">
        <span className="legendSwatch" style={{ background: color }} />
        <strong>{title}</strong>
      </div>
      <p>{children}</p>
    </div>
  );
}

function LegendRow({ color, label }: { color: string; label: string }) {
  return (
    <div className="legendRow">
      <span className="legendSwatch" style={{ background: color }} />
      <span>{label}</span>
    </div>
  );
}

function surfaceLabel(category: string): string {
  return category.replace(/^\w/, (letter) => letter.toUpperCase());
}
