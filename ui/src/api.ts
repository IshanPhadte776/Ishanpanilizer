import type { PanelizeResponse, Settings } from "./types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export async function panelize(settings: Settings): Promise<PanelizeResponse> {
  const response = await fetch(`${API_BASE}/panelize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      input_json: settings.input_json,
      selected_building_indices: settings.selected_building_indices,
      include_building: true,
      settings: {
        panel_width: settings.panel_width,
        panel_height: settings.panel_height,
        cost_per_unique_panel_type: settings.cost_per_unique_panel_type,
        cost_per_panel_element: settings.cost_per_panel_element,
        seed: settings.seed,
        origin_jitter: settings.origin_jitter,
        stagger: settings.stagger,
        include_basement: settings.include_basement,
      },
    }),
  });

  if (!response.ok) {
    throw new Error(await errorDetail(response, "Panelization failed"));
  }

  return response.json();
}

export type UploadResult = {
  input_json: string;
  /** Only present for a .ifc upload -- a CityJSON upload skips extraction entirely. */
  wall_count?: number;
  /** Set when extraction finished with exit 0 but found 0 exterior walls (a naming
   *  convention classify_wall doesn't recognize, not a crash) -- worth surfacing since
   *  panelizing this will otherwise just silently show zero walls with no explanation. */
  warning?: string;
};

export async function uploadModel(file: File): Promise<UploadResult> {
  const body = new FormData();
  body.append("file", file);

  const response = await fetch(`${API_BASE}/upload`, { method: "POST", body });

  if (!response.ok) {
    throw new Error(await errorDetail(response, "Upload failed"));
  }

  return response.json();
}

async function errorDetail(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    // response body wasn't JSON -- fall through to the generic message
  }
  return `${fallback}: ${response.status}`;
}
