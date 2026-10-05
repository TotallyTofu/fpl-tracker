import { useEffect, useState } from "react";

/** Live countdown to an ISO deadline: "4d 19h 40m" (or "19h 40m 05s" inside a day). */
export function useCountdown(deadline: string | null): { text: string; urgent: boolean; passed: boolean } {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (!deadline) return { text: "no deadline", urgent: false, passed: false };
  const diff = new Date(deadline).getTime() - now;
  if (diff <= 0) return { text: "deadline passed", urgent: true, passed: true };
  const d = Math.floor(diff / 86_400_000);
  const h = Math.floor((diff % 86_400_000) / 3_600_000);
  const m = Math.floor((diff % 3_600_000) / 60_000);
  const s = Math.floor((diff % 60_000) / 1000);
  const text = d > 0 ? `${d}d ${h}h ${String(m).padStart(2, "0")}m`
    : `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
  return { text, urgent: diff < 86_400_000, passed: false };
}

export default function Countdown({ deadline }: { deadline: string | null }) {
  const c = useCountdown(deadline);
  return <span className={c.urgent ? "red" : ""}>{c.text}</span>;
}
