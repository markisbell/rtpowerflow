import { useEffect, useState } from "react";
import { api } from "../api";
import type { BatteryMode, BatteryProfiles } from "../types";
import ProfileGraph, { type GLimit } from "./ProfileGraph";

const LABEL: Record<BatteryMode, string> = { self: "Self-sufficiency", peak: "Peak-shaving", price: "Price" };
const COLOR: Record<BatteryMode, string> = { self: "#3fb950", peak: "#f2ae00", price: "#4c8dff" };

// A battery's daily state-of-charge and charge/discharge power (illustrative day
// starting at 50 %); for the price strategy, also the price curve with its
// cheap/expensive thresholds. `now` marks the current time on each graph.
export default function BatteryProfile({ idx, now, day, onClose }: { idx: number; now: number | null; day: number; onClose: () => void }) {
  const [data, setData] = useState<BatteryProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.batteryProfiles(idx).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [idx, day]);

  const color = data ? COLOR[data.mode] : "#3fb950";
  const priceLimits: GLimit[] = data && data.price_lo != null && data.price_hi != null
    ? [{ value: data.price_hi, label: `${data.price_hi.toFixed(0)} (sell)`, color: "#f85149" },
       { value: data.price_lo, label: `${data.price_lo.toFixed(0)} (buy)`, color: "#3fb950" }]
    : [];

  return (
    <div style={{ marginTop: "0.6rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
        <span style={{ fontWeight: 600 }}>
          Battery bus {data?.bus ?? idx} · {data ? LABEL[data.mode] : ""}
        </span>
        <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>
      </div>
      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>error: {err}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>loading…</div>}
      {!err && data && (
        <>
          <div className="muted" style={{ fontSize: "0.68rem" }}>State of charge</div>
          <ProfileGraph series={[{ label: "SOC", color, data: data.soc, fill: true }]}
                        scale={1} unit="%" dec={0} now={now} />
          <div className="muted" style={{ fontSize: "0.68rem", marginTop: 4 }}>Charge (+) / discharge (−)</div>
          <ProfileGraph series={[{ label: "Power", color: "#4c8dff", data: data.power }]}
                        scale={1000} unit="kW" dec={1} baseZero={false} now={now} />
          {data.mode === "price" && data.price.some((v) => v != null) && (
            <>
              <div className="muted" style={{ fontSize: "0.68rem", marginTop: 4 }}>Day-ahead price</div>
              <ProfileGraph series={[{ label: "Price", color: "#f2ae00", data: data.price }]}
                            limits={priceLimits} scale={1} unit="€/MWh" dec={0} now={now} />
            </>
          )}
        </>
      )}
    </div>
  );
}
