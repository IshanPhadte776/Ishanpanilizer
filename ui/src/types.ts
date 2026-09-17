export type Vec3 = [number, number, number];
export type Ring3 = Vec3[];

export type BuildingSurface = {
  category: string;
  semantic_type: string;
  semantic_key: string;
  rings: Ring3[];
  is_basement: boolean;
};

export type BuildingPart = {
  building_id: string;
  parent_id: string;
  surfaces: BuildingSurface[];
};

export type BuildingPayload = {
  parts: BuildingPart[];
};

export type Panel = {
  name: string;
  col: number;
  row: number;
  width: number;
  height: number;
  area: number;
  is_unique: boolean;
  is_residual_width: boolean;
  is_residual_height: boolean;
  is_specialized: boolean;
  n_vertices: number;
  n_pieces: number;
  polygons_xyz: Ring3[][];
};

export type Wall = {
  wall_id: number;
  wall_type: string;
  n_openings: number;
  n_panels: number;
  n_specialized_panels: number;
  panels: Panel[];
};

export type PanelPart = {
  building_id: string;
  parent_id: string;
  total_panels: number;
  total_unique_panels: number;
  total_specialized_panels: number;
  total_unique_types: number;
  walls: Wall[];
};

export type PanelizationPayload = {
  building_id: string;
  summary: {
    n_parts: number;
    n_walls: number;
    total_panels: number;
    total_unique_panels: number;
    total_specialized_panels: number;
    total_unique_types: number;
    cost_total?: number;
    cost_unique_panel_types?: number;
    cost_panel_elements?: number;
  };
  parts: PanelPart[];
  /** Server-side phase split: parsing/triangulating the CityJSON vs generating panels. */
  timings?: {
    model_ms: number;
    panelize_ms: number;
    total_ms: number;
  };
};

export type PanelizeResponse = {
  building?: BuildingPayload;
  panelization: PanelizationPayload;
};

export type Settings = {
  input_json: string;
  selected_building_indices: number[];
  panel_width: number;
  panel_height: number;
  cost_per_unique_panel_type: number;
  cost_per_panel_element: number;
  /** null = plain aligned grid; any number = a reproducible re-rolled layout */
  seed: number | null;
  origin_jitter: number;
  stagger: number;
  /** Below-grade (basement) walls are tagged, not extracted away -- off by default, same
   *  as the old behaviour where they weren't panelizable at all. */
  include_basement: boolean;
};

export type Layers = {
  roof: boolean;
  wall: boolean;
  reveal: boolean;
  balcony: boolean;
  other: boolean;
  panels: boolean;
  specialized: boolean;
};

/** One row of "Compute Panels" history -- every setting and layer visibility toggle that
 *  was in effect, plus every result the Summary bar showed, at the moment it ran. */
export type TrialRecord = {
  trial: number;
  timestamp: string;
  input_json: string;
  selected_building_indices: string;
  panel_width: number;
  panel_height: number;
  cost_per_unique_panel_type: number;
  cost_per_panel_element: number;
  layout: string;
  origin_jitter: number;
  stagger: number;
  include_basement: boolean;
  layer_roof: boolean;
  layer_wall: boolean;
  layer_reveal: boolean;
  layer_balcony: boolean;
  layer_other: boolean;
  layer_panels: boolean;
  layer_specialized: boolean;
  n_walls: number;
  total_panels: number;
  total_unique_panels: number;
  total_specialized_panels: number;
  total_unique_types: number;
  cost_total: number;
  cost_unique_panel_types: number;
  cost_panel_elements: number;
  /** Server: parsing + triangulating the CityJSON. */
  model_load_ms: number;
  /** Server: generating the panels. */
  panelize_ms: number;
  /** Wall-clock for the whole round trip -- the two above plus transfer and JSON parse. */
  duration_ms: number;
};
