import type { Diff, Suggestion } from "../types";

export default function ApplyChecklist({ s }: { s: Suggestion }) {
  const d: Diff = s.diff;
  const names = (ids: number[]) =>
    s.lineup.squad.filter((p) => ids.includes(p.player_id)).map((p) => p.web_name);
  const xiNames = names(s.lineup.xi);
  const bench = s.lineup.bench
    .map((id, i) => `${i + 1} ${s.lineup.squad.find((p) => p.player_id === id)?.web_name ?? "?"}`)
    .join(", ");
  const cap = s.lineup.squad.find((p) => p.player_id === s.lineup.captain)?.web_name ?? "?";
  const vc = s.lineup.squad.find((p) => p.player_id === s.lineup.vice_captain)?.web_name ?? "?";
  const useChip = s.chip_advice.find((c) => c.recommendation === "use");

  const steps: string[] = [];
  if (d.transfers_out.length)
    steps.push(`Transfers — sell first: ${d.transfers_out.map((t) => t.web_name).join(", ")}`);
  if (d.transfers_in.length)
    steps.push(`Buy: ${d.transfers_in.map((t) => t.web_name).join(", ")}`);
  const benchOnly = names(s.lineup.bench);
  if (benchOnly.length) steps.push(`Bench: ${benchOnly.join(", ")}`);
  steps.push(`Start XI: ${xiNames.join(", ")}`);
  steps.push(`Captain: ${cap} — Vice-captain: ${vc}`);
  steps.push(`Bench order: ${bench}`);
  steps.push(useChip ? `Chip: play ${useChip.chip} (${useChip.reason})` : "Chip: none recommended");

  return (
    <details>
      <summary>Apply checklist (manual steps on the official site)</summary>
      <ol className="checklist">
        {steps.map((st, i) => (
          <li key={i}>{st}</li>
        ))}
      </ol>
      <div className="small muted" style={{ marginTop: 6 }}>
        Deadline for GW {s.target_gw}: see the sidebar — do this before it passes.
      </div>
    </details>
  );
}