import { Dices, Download, LayoutGrid, RefreshCw, Save, Upload } from "lucide-react";
import { useRef } from "react";
import type { Layers, Settings, TrialRecord } from "../types";

/** The parts of a run's timing the breakdown tooltip needs -- one run, or several summed. */
type Timing = Pick<TrialRecord, "model_load_ms" | "panelize_ms" | "duration_ms">;

type ControlsProps = {
  settings: Settings;
  layers: Layers;
  loading: boolean;
  onSettingsChange: (settings: Settings) => void;
  onLayersChange: (layers: Layers) => void;
  onPanelize: () => void;
  onReroll: () => void;
  onAlign: () => void;
  trialCount: number;
  latestTrial?: Timing;
  trialTotals: Timing;
  onExportAllTrials: () => void;
  onExportLatestTrial: () => void;
  onUploadModel: (file: File) => void;
  /** Set only while an upload is actually in flight -- shown on the button in place of its
   *  normal label, since a .ifc upload can take minutes and a plain disabled state alone
   *  doesn't say why. */
  uploadStatus?: string;
};

export function Controls({
  settings,
  layers,
  loading,
  onSettingsChange,
  onLayersChange,
  onPanelize,
  onReroll,
  onAlign,
  trialCount,
  latestTrial,
  trialTotals,
  onExportAllTrials,
  onExportLatestTrial,
  onUploadModel,
  uploadStatus,
}: ControlsProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <aside className="controls">
      <div className="brand">
        <span>Panilizer</span>
      </div>

      <label className="field">
        <span>Input JSON</span>
        <input
          value={settings.input_json}
          onChange={(event) => onSettingsChange({ ...settings, input_json: event.target.value })}
        />
      </label>

      <input
        ref={fileInputRef}
        type="file"
        accept=".json,.ifc,application/json"
        className="visuallyHidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = ""; // clear so re-selecting the same file still fires onChange
          if (file) onUploadModel(file);
        }}
      />
      <button
        className="secondaryButton"
        type="button"
        onClick={() => fileInputRef.current?.click()}
        disabled={loading}
        title={
          uploadStatus
            ? "Large buildings can take several minutes to convert -- the server is still working"
            : "Upload a CityJSON model.json, or a raw .ifc (converted + sanitized on the server first), and panelize it immediately"
        }
      >
        <Upload size={18} />
        <span>{uploadStatus ?? "Upload model & Panelize"}</span>
      </button>

      <div className="sliderGroup">
        <Slider
          label="Panel width"
          value={settings.panel_width}
          min={0.4}
          max={3.0}
          step={0.1}
          unit="m"
          onChange={(panel_width) => onSettingsChange({ ...settings, panel_width })}
        />
        <Slider
          label="Panel height"
          value={settings.panel_height}
          min={0.6}
          max={4.0}
          step={0.1}
          unit="m"
          onChange={(panel_height) => onSettingsChange({ ...settings, panel_height })}
        />
        <Slider
          label="Cost per type"
          value={settings.cost_per_unique_panel_type}
          min={0}
          max={1000}
          step={25}
          unit="CAD"
          decimals={0}
          onChange={(cost_per_unique_panel_type) => onSettingsChange({ ...settings, cost_per_unique_panel_type })}
        />
        <Slider
          label="Cost per element"
          value={settings.cost_per_panel_element}
          min={0}
          max={250}
          step={5}
          unit="CAD"
          decimals={0}
          onChange={(cost_per_panel_element) => onSettingsChange({ ...settings, cost_per_panel_element })}
        />
      </div>

      <label className="toggle" title="Below-grade walls are excluded by default -- they're detected automatically as the storey(s) at the bottom of the building with no windows or curtain walls.">
        <span>Include basement</span>
        <input
          type="checkbox"
          checked={settings.include_basement}
          onChange={(event) => onSettingsChange({ ...settings, include_basement: event.target.checked })}
        />
      </label>

      <button className="primaryButton" onClick={onPanelize} disabled={loading}>
        <RefreshCw size={18} />
        <span>{loading ? "Computing" : "Compute Panels"}</span>
      </button>

      <div className="layoutGroup">
        <h2>Layout</h2>
        <div className="buttonRow">
          <button className="secondaryButton" onClick={onReroll} disabled={loading}>
            <Dices size={16} />
            <span>Re-roll</span>
          </button>
          <button
            className="secondaryButton"
            onClick={onAlign}
            disabled={loading || settings.seed === null}
          >
            <LayoutGrid size={16} />
            <span>Aligned</span>
          </button>
        </div>
        <p className="layoutHint">
          {settings.seed === null
            ? "Aligned grid — cheapest, every course starts at the wall edge."
            : `Seed ${settings.seed} — re-rolled. Same seed always gives this layout.`}
        </p>
      </div>

      <div className="layers">
        <h2>Layers</h2>
        {Object.entries(layers).map(([key, value]) => (
          <label key={key} className="toggle">
            <span>{layerLabel(key)}</span>
            <input
              type="checkbox"
              checked={value}
              onChange={(event) => onLayersChange({ ...layers, [key]: event.target.checked })}
            />
          </label>
        ))}
      </div>

      <div className="buttonRow">
        <button
          className="secondaryButton"
          type="button"
          onClick={onExportLatestTrial}
          disabled={trialCount === 0}
          title={
            trialCount === 0
              ? "Run Compute Panels at least once first"
              : `Download just the most recent run (trial ${trialCount}) as one CSV row`
          }
        >
          <Download size={16} />
          <span>Latest run</span>
        </button>
        <button
          className="secondaryButton"
          type="button"
          onClick={onExportAllTrials}
          disabled={trialCount === 0}
          title={
            trialCount === 0
              ? "Run Compute Panels at least once first"
              : `Download every trial run this session as a CSV row (${trialCount} so far)`
          }
        >
          <Download size={16} />
          <span>All trials{trialCount > 0 ? ` (${trialCount})` : ""}</span>
        </button>
      </div>

      {trialCount > 0 && latestTrial && (
        <div className="trialTimes">
          <TrialTime timing={latestTrial} />
          <TrialTime timing={trialTotals} suffix=" total" runs={trialCount} />
        </div>
      )}

      <button className="secondaryButton" type="button" disabled>
        <Save size={18} />
        <span>Export via API</span>
      </button>
    </aside>
  );
}

type SliderProps = {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  decimals?: number;
  onChange: (value: number) => void;
};

function Slider({ label, value, min, max, step, unit, decimals = 1, onChange }: SliderProps) {
  return (
    <label className="slider">
      <span>
        {label}
        <strong>
          {unit === "CAD" ? "CAD " : ""}
          {value.toFixed(decimals)}
          {unit !== "CAD" ? ` ${unit}` : ""}
        </strong>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

function formatDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

/** Shows model+panelize as the headline figure, with the phase split on hover.
 *
 *  The headline is the server's own work (model load + panelize) rather than the round
 *  trip, because that's the part the settings actually move. Transfer is the remainder --
 *  serialising and shipping several MB of panel geometry -- and it's broken out separately
 *  so a slow run can be attributed to the right thing instead of guessed at. */
function TrialTime({ timing, suffix = "", runs }: { timing: Timing; suffix?: string; runs?: number }) {
  const serverMs = timing.model_load_ms + timing.panelize_ms;
  // clamp: the two clocks are measured on different sides of the wire, so tiny negatives are possible
  const transferMs = Math.max(0, timing.duration_ms - serverMs);

  return (
    <span className="trialTime">
      {formatDuration(serverMs)}
      {suffix}
      <span className="trialTimeTip">
        {runs !== undefined && <em>across {runs} run{runs === 1 ? "" : "s"}</em>}
        <span><span>Model load</span><span>{formatDuration(timing.model_load_ms)}</span></span>
        <span><span>Panelize</span><span>{formatDuration(timing.panelize_ms)}</span></span>
        <span><span>Transfer</span><span>{formatDuration(transferMs)}</span></span>
        <span className="trialTimeTotal">
          <span>Round trip</span><span>{formatDuration(timing.duration_ms)}</span>
        </span>
      </span>
    </span>
  );
}

function layerLabel(key: string) {
  return key
    .replace("specialized", "specialized panels")
    .replace(/^\w/, (letter) => letter.toUpperCase());
}
