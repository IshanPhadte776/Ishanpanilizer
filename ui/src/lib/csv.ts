import type { TrialRecord } from "../types";

/** Escapes one CSV field: wraps in quotes (doubling any inner quotes) whenever the raw
 *  value contains a comma, quote, or newline -- the minimum RFC 4180 requires. */
function csvField(value: string | number | boolean): string {
  const text = String(value);
  if (/[",\n]/.test(text)) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

export function trialsToCsv(trials: TrialRecord[]): string {
  if (trials.length === 0) return "";
  const columns = Object.keys(trials[0]) as (keyof TrialRecord)[];
  const lines = [columns.join(",")];
  for (const trial of trials) {
    lines.push(columns.map((column) => csvField(trial[column])).join(","));
  }
  // CRLF is the RFC 4180 line ending and what Excel expects without a BOM dance
  return lines.join("\r\n");
}

export function downloadCsv(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
