// Shared helpers for presenting news signals.
import type { Signal } from "./types";

const SOURCE_NAME: Record<string, string> = {
  "fpl-official": "Official FPL",
  bbc: "BBC",
  espn: "ESPN",
  reddit: "Reddit",
  youtube: "YouTube",
};

export function signalOrigin(s: Signal): "official" | "llm" | "keyword" {
  if (s.source.startsWith("fpl-official")) return "official";
  return (s.model || "").startsWith("llm") ? "llm" : "keyword";
}

/** "Official FPL" | "BBC, read by LLM" | "Reddit, keyword match" */
export function signalSourceLabel(s: Signal): string {
  const src = SOURCE_NAME[s.source.split(":")[0]] ?? s.source.split(":")[0];
  const origin = signalOrigin(s);
  if (origin === "official") return "Official FPL";
  return `${src}, ${origin === "llm" ? "read by LLM" : "keyword match"}`;
}

/** Effect on the projection (official news is already inside FPL's numbers). */
export function signalEffect(s: Signal): string {
  if (signalOrigin(s) === "official") return "already in FPL's projection";
  if (s.sentiment === "negative") return `projection −${Math.round(50 * s.confidence)}%`;
  if (s.sentiment === "positive") return `projection +${Math.round(10 * s.confidence)}%`;
  return "no effect";
}

export function signalMark(s: Signal): { cls: string; text: string } {
  if (signalOrigin(s) === "keyword" && s.confidence < 0.5) return { cls: "sus", text: "?" };
  if (s.sentiment === "negative") return { cls: "neg", text: "−" };
  if (s.sentiment === "positive") return { cls: "pos", text: "+" };
  return { cls: "neu", text: "·" };
}

export function shortDate(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** Signals that move projections first, then newest, then most confident. */
export function sortSignals(list: Signal[]): Signal[] {
  const noEffect = (s: Signal) => (s.sentiment === "neutral" ? 1 : 0);
  const when = (s: Signal) => new Date(s.published_at ?? s.retrieved_at).getTime() || 0;
  return [...list].sort((a, b) => noEffect(a) - noEffect(b) || when(b) - when(a) || b.confidence - a.confidence);
}
