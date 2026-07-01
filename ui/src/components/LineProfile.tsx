import { useEffect, useState } from "react";
import { api } from "../api";
import type { LineProfiles } from "../types";
import ProfileGraph from "./ProfileGraph";

// Per-line daily current graph with the line's rated current (ampacity) limit.
export default function LineProfile({ line, name, onClose }: { line: number; name: string; onClose: () => void }) {
  const [data, setData] = useState<LineProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.lineProfiles(line).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [line]);

  const hasData = (data?.current?.length ?? 0) > 0 && data!.current.some((v) => v != null);

  return (
    <div style={{ marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
        <span style={{ fontWeight: 600 }}>Line {name}</span>
        <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>
      </div>
      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>error: {err}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>loading…</div>}
      {!err && data && hasData && (
        <ProfileGraph
          series={[{ label: "Current", color: "#4c8dff", data: data.current, fill: true }]}
          limits={data.rated_i_ka != null
            ? [{ value: data.rated_i_ka, label: `rated ${(data.rated_i_ka * 1000).toFixed(0)} A`, color: "#f85149" }]
            : []}
          scale={1000} unit="A" dec={0}
        />
      )}
      {!err && data && !hasData && (
        <div className="muted" style={{ fontSize: "0.72rem" }}>No current (line out of service?).</div>
      )}
    </div>
  );
}
