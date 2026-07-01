import { useEffect, useState } from "react";
import { api } from "../api";
import type { TrafoProfiles } from "../types";
import { loadingColor } from "../scales";
import ProfileGraph, { type GLimit } from "./ProfileGraph";

// Per-transformer daily power-exchange graph (HV-side P) with the transformer's
// rated apparent power as the capacity limit. When the flow reverses (PV export),
// the axis spans ±rated; otherwise it's an import-only 0..rated view.
export default function TrafoProfile({ trafo, name, now, onClose }: { trafo: number; name: string; now: number | null; onClose: () => void }) {
  const [data, setData] = useState<TrafoProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.trafoProfiles(trafo).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [trafo]);

  const power = data?.power ?? [];
  const hasData = power.length > 0 && power.some((v) => v != null);
  const hasExport = power.some((v) => v != null && v < 0);
  const sn = data?.sn_mva ?? null;
  const limits: GLimit[] = sn == null ? [] :
    hasExport
      ? [{ value: sn, label: `rated ${(sn * 1000).toFixed(0)} kVA`, color: "#f85149" },
         { value: -sn, label: `−${(sn * 1000).toFixed(0)} kVA`, color: "#f85149" }]
      : [{ value: sn, label: `rated ${(sn * 1000).toFixed(0)} kVA`, color: "#f85149" }];

  return (
    <div style={{ marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
        <span style={{ fontWeight: 600 }}>Transformer {name}</span>
        <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>
      </div>
      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>error: {err}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>loading…</div>}
      {!err && data && hasData && (
        <ProfileGraph
          series={[{ label: "P exchange", color: "#f2ae00", data: power, fill: !hasExport,
                     colorData: data.loading, colorFn: loadingColor }]}
          limits={limits} scale={1000} unit="kW" dec={1} baseZero={!hasExport} now={now}
        />
      )}
      {!err && data && !hasData && (
        <div className="muted" style={{ fontSize: "0.72rem" }}>No power data (transformer out of service?).</div>
      )}
    </div>
  );
}
