import { useCallback, useEffect, useState } from "react";
import { panelize, uploadModel } from "./api";
import { Controls } from "./components/Controls";
import { Summary } from "./components/Summary";
import { Viewer } from "./components/Viewer";
import { downloadCsv, trialsToCsv } from "./lib/csv";
import type { Layers, PanelizeResponse, Settings, TrialRecord } from "./types";

const initialSettings: Settings = {
  input_json: "input/GLENGARRY_HOUSE/model.json",
  selected_building_indices: [0],
  panel_width: 1.2,
  panel_height: 2.4,
  cost_per_unique_panel_type: 250,
  cost_per_panel_element: 45,
  seed: null,
  origin_jitter: 0.3,
  stagger: 0,
  include_basement: false,
};

const initialLayers: Layers = {
  roof: true,
  wall: true,
  reveal: true,
  balcony: true,
  other: false,
  panels: true,
  specialized: true,
};

export default function App() {
  const [settings, setSettings] = useState(initialSettings);
  const [layers, setLayers] = useState(initialLayers);
  const [data, setData] = useState<PanelizeResponse>();
  const [trials, setTrials] = useState<TrialRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>();
  const [uploadWarning, setUploadWarning] = useState<string>();
  // Only meaningful while an upload is in flight -- distinct from the generic "Computing"
  // label because a .ifc upload runs the extraction pipeline server-side and can take
  // minutes, not the ~1-5s a normal Compute Panels click takes.
  const [uploadStatus, setUploadStatus] = useState<string>();
  // Sticky: only an upload updates this (not a plain Compute Panels/Re-roll/Aligned click),
  // so it keeps showing "last time you uploaded a model, this is what the whole thing cost"
  // rather than disappearing the moment you tweak a slider afterward.
  const [uploadPlusPanelizeMs, setUploadPlusPanelizeMs] = useState<number>();

  // Takes an explicit override so re-roll can compute with the new seed immediately --
  // setSettings is async, so reading it back on the next line would use the stale value.
  // Returns the round-trip duration (or undefined on failure) so uploadAndCompute can add
  // it to the upload's own time for a combined "upload model + panelize" total.
  const compute = useCallback(async (override?: Settings): Promise<number | undefined> => {
    const activeSettings = override ?? settings;
    setLoading(true);
    setError(undefined);
    const startedAt = performance.now();
    try {
      const response = await panelize(activeSettings);
      const durationMs = performance.now() - startedAt;
      setData(response);

      // Snapshot every setting/layer visibility toggle and every result the Summary bar
      // shows, exactly as they stood for this run -- so "Export CSV" is a real run log,
      // not just whatever the form happens to hold at export time.
      const summary = response.panelization.summary;
      setTrials((previous) => [
        ...previous,
        {
          trial: previous.length + 1,
          timestamp: new Date().toISOString(),
          input_json: activeSettings.input_json,
          selected_building_indices: activeSettings.selected_building_indices.join(";"),
          panel_width: activeSettings.panel_width,
          panel_height: activeSettings.panel_height,
          cost_per_unique_panel_type: activeSettings.cost_per_unique_panel_type,
          cost_per_panel_element: activeSettings.cost_per_panel_element,
          layout: activeSettings.seed === null ? "aligned" : `seed-${activeSettings.seed}`,
          origin_jitter: activeSettings.origin_jitter,
          stagger: activeSettings.stagger,
          include_basement: activeSettings.include_basement,
          layer_roof: layers.roof,
          layer_wall: layers.wall,
          layer_reveal: layers.reveal,
          layer_balcony: layers.balcony,
          layer_other: layers.other,
          layer_panels: layers.panels,
          layer_specialized: layers.specialized,
          n_walls: summary.n_walls,
          total_panels: summary.total_panels,
          total_unique_panels: summary.total_unique_panels,
          total_specialized_panels: summary.total_specialized_panels,
          total_unique_types: summary.total_unique_types,
          cost_total: summary.cost_total ?? 0,
          cost_unique_panel_types: summary.cost_unique_panel_types ?? 0,
          cost_panel_elements: summary.cost_panel_elements ?? 0,
          model_load_ms: Math.round(response.panelization.timings?.model_ms ?? 0),
          panelize_ms: Math.round(response.panelization.timings?.panelize_ms ?? 0),
          duration_ms: Math.round(durationMs),
        },
      ]);
      return durationMs;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Panelization failed");
      return undefined;
    } finally {
      setLoading(false);
    }
  }, [settings, layers]);

  const exportAllTrials = useCallback(() => {
    if (trials.length === 0) return;
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(`panilizer-trials-${stamp}.csv`, trialsToCsv(trials));
  }, [trials]);

  const exportLatestTrial = useCallback(() => {
    if (trials.length === 0) return;
    const latest = trials[trials.length - 1];
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    downloadCsv(`panilizer-trial-${latest.trial}-${stamp}.csv`, trialsToCsv([latest]));
  }, [trials]);

  const applyLayout = useCallback((seed: number | null) => {
    const next = { ...settings, seed };
    setSettings(next);
    void compute(next);
  }, [settings, compute]);

  // Upload a CityJSON model.json OR a raw .ifc (the server runs ifc_to_cityjson.py +
  // sanitize_cityjson.py first for a .ifc), point settings at wherever it landed, then
  // panelize that immediately -- one action instead of "upload, copy the path, paste it
  // into Input JSON, click Compute".
  const uploadAndCompute = useCallback(async (file: File) => {
    setLoading(true);
    setError(undefined);
    setUploadWarning(undefined);
    setUploadStatus(file.name.toLowerCase().endsWith(".ifc") ? "Converting IFC…" : "Uploading…");
    const uploadStartedAt = performance.now();
    try {
      const result = await uploadModel(file);
      const uploadMs = performance.now() - uploadStartedAt;
      // Kept separate from `error`: compute() below resets `error` at its own start (and
      // only sets a new one if IT fails), so a warning stored there would be wiped out
      // the instant the subsequent compute() call begins, before ever reaching the screen.
      if (result.warning) setUploadWarning(result.warning);
      const next = { ...settings, input_json: result.input_json };
      setSettings(next);
      const panelizeMs = await compute(next);
      if (panelizeMs !== undefined) setUploadPlusPanelizeMs(uploadMs + panelizeMs);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
      setLoading(false);
    } finally {
      setUploadStatus(undefined);
    }
  }, [settings, compute]);

  useEffect(() => {
    void compute();
  }, []);

  return (
    <main className="appShell">
      <Controls
        settings={settings}
        layers={layers}
        loading={loading}
        onSettingsChange={setSettings}
        onLayersChange={setLayers}
        onPanelize={() => compute()}
        onReroll={() => applyLayout(Math.floor(Math.random() * 100000))}
        onAlign={() => applyLayout(null)}
        trialCount={trials.length}
        latestTrial={trials.length > 0 ? trials[trials.length - 1] : undefined}
        trialTotals={trials.reduce(
          (sum, trial) => ({
            model_load_ms: sum.model_load_ms + trial.model_load_ms,
            panelize_ms: sum.panelize_ms + trial.panelize_ms,
            duration_ms: sum.duration_ms + trial.duration_ms,
          }),
          { model_load_ms: 0, panelize_ms: 0, duration_ms: 0 },
        )}
        onExportAllTrials={exportAllTrials}
        onExportLatestTrial={exportLatestTrial}
        onUploadModel={uploadAndCompute}
        uploadStatus={uploadStatus}
      />
      <section className="workspace">
        <div className="viewerFrame">
          <Viewer
            building={data?.building}
            panelization={data?.panelization}
            layers={layers}
            includeBasement={settings.include_basement}
          />
        </div>
        <Summary
          panelization={data?.panelization}
          settings={settings}
          error={error}
          warning={uploadWarning}
          panelizeMs={trials.length > 0 ? trials[trials.length - 1].duration_ms : undefined}
          uploadPlusPanelizeMs={uploadPlusPanelizeMs}
        />
      </section>
    </main>
  );
}
