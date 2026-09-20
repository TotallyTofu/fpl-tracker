import { useEffect, useState } from "react";

export default function Countdown({ deadline }: { deadline: string | null }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  if (!deadline) return <span className="muted">no deadline</span>;
  const target = new Date(deadline).getTime();
  const diff = target - now;
  if (diff <= 0) return <span className="red">deadline passed</span>;
  const h = Math.floor(diff / 3600000);
  const m = Math.floor((diff % 3600000) / 60000);
  const s = Math.floor((diff % 60000) / 1000);
  const urgent = diff < 24 * 3600000;
  return (
    <span className={`countdown ${urgent ? "urgent" : ""}`}>
      {h}h {String(m).padStart(2, "0")}m {String(s).padStart(2, "0")}s
    </span>
  );
}