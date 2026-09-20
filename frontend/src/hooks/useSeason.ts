import { useEffect, useState } from "react";
import { api } from "../api";
import type { Season } from "../types";

export function useSeason(pollMs = 60_000): { season: Season | null; error: string | null } {
  const [season, setSeason] = useState<Season | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .getSeason()
        .then((s) => {
          if (alive) {
            setSeason(s);
            setError(null);
          }
        })
        .catch((e) => alive && setError(String(e)));
    load();
    const t = setInterval(load, pollMs);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [pollMs]);

  return { season, error };
}