import { NavLink, Route, Routes } from "react-router-dom";
import StartupToast from "./components/StartupToast";
import Dashboard from "./pages/Dashboard";
import MyTeam from "./pages/MyTeam";
import News from "./pages/News";
import Settings from "./pages/Settings";
import Suggestions from "./pages/Suggestions";
import { useSeason } from "./hooks/useSeason";
import { fmtTime } from "./types";

const NAV = [
  { to: "/", label: "Dashboard" },
  { to: "/team", label: "My Team" },
  { to: "/suggestions", label: "Suggestions" },
  { to: "/news", label: "News" },
  { to: "/settings", label: "Settings" },
];

export default function App() {
  const { season } = useSeason();
  return (
    <div className="layout">
      <StartupToast />
      <aside className="sidebar">
        <div className="logo">⚽ FPL Tracker</div>
        <nav>
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => (isActive ? "nav active" : "nav")}>
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          {season?.season ? (
            <>
              <div className="season">{season.season}</div>
              <div className="gw">
                GW {season.current_gw ?? "—"} / {season.events_total || "?"}
              </div>
              {season.next_gw && (
                <div className="deadline" title={season.deadline ? fmtTime(season.deadline) : ""}>
                  Next: GW {season.next_gw}
                  <br />
                  <span className={season.deadline_is_past ? "red" : ""}>
                    {season.deadline ? fmtTime(season.deadline) : ""}
                  </span>
                </div>
              )}
            </>
          ) : (
            <div className="season">no season data</div>
          )}
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/team" element={<MyTeam />} />
          <Route path="/suggestions" element={<Suggestions />} />
          <Route path="/news" element={<News />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}