import { useEffect, useState } from "react";
import { api } from "../api";
import type { NodeProfiles, NodeSeriesKind } from "../types";
import { voltageColor } from "../scales";
import ProfileGraph, { type GSeries } from "./ProfileGraph";

const COLOR: Record<NodeSeriesKind, string> = { residential: "#4c8dff", ev: "#f2ae00", pv: "#3fb950" };
const LABEL: Record<NodeSeriesKind, string> = { residential: "Residential", ev: "EV", pv: "PV" };
const VLIMIT = [
  { value: 1.1, label: "1.10 pu", color: "#f85149" },
  { value: 0.9, label: "0.90 pu", color: "#f85149" },
];

// Per-node daily graph: power (residential/EV/PV load & generation) or voltage,
// switchable, with the EN 50160 ±10 % voltage limits shown in voltage mode.
export default function NodeProfile({ bus, name, now, day, onClose }: { bus: number; name: string; now: number | null; day: number; onClose: () => void }) {
  const [data, setData] = useState<NodeProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<"power" | "voltage">("power");

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.nodeProfiles(bus).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [bus, day]);

  const powerSeries: GSeries[] = (data?.series ?? []).map((s) => ({
    label: LABEL[s.kind], color: COLOR[s.kind], data: s.p_mw, fill: true,
  }));
  const hasVoltage = (data?.voltage?.length ?? 0) > 0;

  return (
    <div style={{ marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
        <span style={{ fontWeight: 600 }}>Node {name}</span>
        <span style={{ display: "flex", gap: 4, alignItems: "center" }}>
          <button className={mode === "power" ? "on" : ""} style={{ fontSize: "0.68rem", padding: "1px 6px" }} onClick={() => setMode("power")}>Power</button>
          <button className={mode === "voltage" ? "on" : ""} style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                  onClick={() => setMode("voltage")} disabled={!hasVoltage}>Voltage</button>
          <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>
        </span>
      </div>

      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>error: {err}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>loading…</div>}
      {!err && data && mode === "power" && (
        powerSeries.length
          ? <ProfileGraph series={powerSeries} scale={1000} unit="kW" dec={1} now={now} />
          : <div className="muted" style={{ fontSize: "0.72rem" }}>No load or generation at this node.</div>
      )}
      {!err && data && mode === "voltage" && hasVoltage && (
        <ProfileGraph
          series={[{ label: "Voltage", color: "#c586ff", data: data.voltage, colorData: data.voltage, colorFn: voltageColor }]}
          limits={VLIMIT} scale={1} unit="pu" dec={3} baseZero={false} now={now}
        />
      )}
    </div>
  );
}
