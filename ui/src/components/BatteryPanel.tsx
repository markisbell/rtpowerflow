import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { Battery, BatteryMode } from "../types";

const MODE_COLOR: Record<BatteryMode, string> = { self: "#3fb950", peak: "#f2ae00", price: "#4c8dff" };

interface LiveState { soc_percent: number; p_mw: number }

// Manage local batteries: add one at the selected node/transformer, list them
// with live SOC + charge/discharge power, and remove.
export default function BatteryPanel({
  batteries, live, modes, hasPrices, addBus, addIsTrafo, onAdd, onRemove, onSelect, selectedIdx = null,
}: {
  batteries: Battery[];
  live: Record<number, LiveState>;
  modes: BatteryMode[];
  hasPrices: boolean;
  addBus: number | null;
  addIsTrafo: boolean;
  onAdd: (bus: number, capacity_kwh: number, power_kw: number, mode: BatteryMode) => void;
  onRemove: (idx: number) => void;
  onSelect?: (idx: number) => void;
  selectedIdx?: number | null;
}) {
  const { t } = useTranslation();
  const usable = modes.filter((m) => m !== "price" || hasPrices);
  const [mode, setMode] = useState<BatteryMode>(addIsTrafo ? "peak" : "self");
  const [cap, setCap] = useState(addIsTrafo ? 100 : 10);
  const [pow, setPow] = useState(addIsTrafo ? 50 : 5);

  return (
    <div style={{ marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ fontWeight: 600, fontSize: "0.82rem", marginBottom: 4 }}>{t("bat.heading")}</div>

      {batteries.map((b) => {
        const st = live[b.index];
        const soc = st?.soc_percent ?? b.soc_percent;
        const p = (st?.p_mw ?? 0) * 1000;
        return (
          <div key={b.index} onClick={() => onSelect?.(b.index)} title={t("bat.rowTitle")}
               style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.72rem", margin: "2px 0",
                        cursor: "pointer", borderRadius: 4, padding: "1px 3px",
                        background: b.index === selectedIdx ? "#1b2430" : "transparent" }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: MODE_COLOR[b.mode], flex: "0 0 auto" }} />
            <span style={{ flex: "0 0 auto" }}>{t("bat.busN", { bus: b.bus })}</span>
            <span className="muted" style={{ flex: "0 0 auto" }}>{t(`bat.${b.mode}`)}</span>
            <span style={{ flex: 1, height: 7, background: "#1b2028", borderRadius: 4, overflow: "hidden" }}>
              <span style={{ display: "block", height: "100%", width: `${Math.max(0, Math.min(100, soc))}%`, background: MODE_COLOR[b.mode] }} />
            </span>
            <span style={{ flex: "0 0 auto", fontVariantNumeric: "tabular-nums" }}>{soc.toFixed(0)}%</span>
            <span style={{ flex: "0 0 46px", textAlign: "right", fontVariantNumeric: "tabular-nums", color: p > 0.05 ? "#3fb950" : p < -0.05 ? "#f2ae00" : "var(--muted)" }}>
              {p >= 0 ? "+" : ""}{p.toFixed(1)}kW
            </span>
            <button className="ghost" style={{ padding: "0 5px", fontSize: "0.7rem" }}
                    onClick={(e) => { e.stopPropagation(); onRemove(b.index); }}>✕</button>
          </div>
        );
      })}
      {batteries.length === 0 && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("bat.none")}</div>}

      {addBus == null ? (
        <p className="muted" style={{ fontSize: "0.72rem", marginTop: 5 }}>{t("bat.addHint")}</p>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 5, marginTop: 6, fontSize: "0.72rem" }}>
          <select value={mode} onChange={(e) => setMode(e.target.value as BatteryMode)} style={{ fontSize: "0.72rem" }}>
            {usable.map((m) => <option key={m} value={m}>{t(`bat.${m}`)}</option>)}
          </select>
          <input type="number" min={1} step={1} value={cap} onChange={(e) => setCap(+e.target.value)}
                 style={{ width: 52, fontSize: "0.72rem" }} /> kWh
          <input type="number" min={1} step={1} value={pow} onChange={(e) => setPow(+e.target.value)}
                 style={{ width: 46, fontSize: "0.72rem" }} /> kW
          <button className="primary" style={{ padding: "2px 8px", fontSize: "0.72rem" }}
                  onClick={() => onAdd(addBus, cap, pow, mode)}>
            {t("bat.add", { bus: addBus })}
          </button>
        </div>
      )}
    </div>
  );
}
