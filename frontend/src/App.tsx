import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { useCountdown } from "./components/Countdown";
import StartupToast from "./components/StartupToast";
import { useSeason } from "./hooks/useSeason";
import Dashboard from "./pages/Dashboard";
import MyTeam from "./pages/MyTeam";
import News from "./pages/News";
import Settings from "./pages/Settings";
import Suggestions from "./pages/Suggestions";

const NAV = [
  { to: "/", label: "This gameweek" },
  { to: "/team", label: "My team" },
  { to: "/suggestions", label: "Transfer plans" },
  { to: "/news", label: "News & signals" },
  { to: "/settings", label: "Settings" },
];

type Theme = "auto" | "light" | "dark";
const THEME_KEY = "fpl-theme";

function readTheme(): Theme {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "dark" ? t : "auto";
  } catch {
    return "auto";
  }
}

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* private mode: theme just isn't remembered */
    }
  }, [theme]);
  const next = () => setTheme((t) => (t === "auto" ? "light" : t === "light" ? "dark" : "auto"));
  return [theme, next];
}

function DeadlinePill() {
  const { season } = useSeason();
  const c = useCountdown(season?.deadline ?? null);
  if (!season?.next_gw) return null;
  return (
    <span className={`deadline-pill ${c.urgent ? "urgent" : ""}`} title={season.deadline ?? ""}>
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <circle cx="8" cy="8" r="6.5" />
        <path d="M8 4.5V8l2.5 1.5" />
      </svg>
      GW{season.next_gw} deadline {c.passed ? "passed" : <>in {c.text}</>}
    </span>
  );
}

export default function App() {
  const [theme, nextTheme] = useTheme();
  return (
    <>
      <header className="topbar">
        <div className="topbar-art" aria-hidden="true" />
        <div className="topbar-inner">
          <div className="topbar-row">
            <NavLink to="/" className="brand" aria-label="FPL Tracker home">
              <svg className="brand-mark" width="46" height="46" viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinejoin="round" aria-hidden="true">
                <rect x="2" y="5" width="24" height="18" rx="3" />
                <line x1="14" y1="5" x2="14" y2="23" />
                <circle cx="14" cy="14" r="3.5" />
              </svg>
              <span className="brand-word">FPL Tracker</span>
            </NavLink>
          </div>
          <nav className="mainnav" aria-label="Main">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
                {n.label}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="topbar-strip">
          <div className="topbar-strip-inner">
            <DeadlinePill />
            <button type="button" className="theme-btn" onClick={nextTheme} aria-label={`Theme: ${theme}. Change theme`}>
              {theme === "auto" ? "Auto" : theme === "light" ? "Light" : "Dark"}
            </button>
          </div>
        </div>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/team" element={<MyTeam />} />
          <Route path="/suggestions" element={<Suggestions />} />
          <Route path="/news" element={<News />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
      <StartupToast />
    </>
  );
}
