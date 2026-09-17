import type { PanelizationPayload, Settings } from "../types";

type SummaryProps = {
  panelization?: PanelizationPayload;
  settings: Settings;
  error?: string;
  /** Set when the last upload succeeded but is worth a second look (e.g. an IFC extracted
   *  with 0 exterior walls) -- shown alongside the metrics rather than replacing them,
   *  since compute() did genuinely run and the numbers below are real. */
  warning?: string;
  /** Round-trip time for the most recent /panelize call, whatever triggered it -- Compute
   *  Panels, Re-roll, Aligned, or the tail end of an upload. */
  panelizeMs?: number;
  /** Upload time + the panelize call right after it, combined -- only set once an upload
   *  has actually happened this session, and sticky (a later plain Compute Panels click
   *  doesn't clear it) so it keeps answering "what did the last upload actually cost". */
  uploadPlusPanelizeMs?: number;
};

export function Summary({ panelization, settings, error, warning, panelizeMs, uploadPlusPanelizeMs }: SummaryProps) {
  if (error) {
    return <section className="summary error">{error}</section>;
  }

  if (!panelization) {
    return <section className="summary">Waiting for panel data</section>;
  }

  const summary = panelization.summary;
  const fallbackCost =
    summary.total_unique_types * settings.cost_per_unique_panel_type
    + summary.total_panels * settings.cost_per_panel_element;
  const totalCost =
    typeof summary.cost_total === "number" && Number.isFinite(summary.cost_total)
      ? summary.cost_total
      : fallbackCost;

  return (
    <section className="summary">
      {warning && <div className="summaryWarning">{warning}</div>}
      <Metric label="Walls" value={summary.n_walls} />
      <Metric label="Panels" value={summary.total_panels} />
      <Metric label="Unique" value={summary.total_unique_panels} />
      <Metric label="Specialized" value={summary.total_specialized_panels} />
      <Metric label="Cost" value={totalCost} prefix="CAD " />
      {panelizeMs !== undefined && <TimeMetric label="Panelize" value={panelizeMs} />}
      {uploadPlusPanelizeMs !== undefined && <TimeMetric label="Upload + Panelize" value={uploadPlusPanelizeMs} />}
    </section>
  );
}

function Metric({ label, value, prefix = "" }: { label: string; value: number; prefix?: string }) {
  const displayValue = Number.isFinite(value) ? Math.round(value).toLocaleString() : "0";
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{prefix}{displayValue}</strong>
    </div>
  );
}

function TimeMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{formatDuration(value)}</strong>
    </div>
  );
}

function formatDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}
