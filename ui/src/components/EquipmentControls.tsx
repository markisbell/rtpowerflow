/**
 * Small equipment input controls + the Stat row (extracted from
 * LivePowerFlow, 2026-07-10): battery sizing, controller limit, rONT
 * voltage target. Pure presentational — every change goes up via callback.
 */
import { useEffect, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import type { Battery, GridController, RontInfo } from "../types";
import { V_BASE } from "../scales";

// classic home-storage units (kWh · kW); the busbar battery is sized freely
const HOME_SIZES: [number, number][] = [[5, 2.5], [10, 5], [15, 7.5], [20, 10]];
const numStyle: CSSProperties = {
  width: 64, fontSize: "0.72rem", background: "var(--panel-2)",
  color: "var(--text)", border: "1px solid var(--border)", borderRadius: 4,
  padding: "1px 4px",
};

export function BatterySize({ bat, free, onSize }: {
  bat: Battery; free: boolean; onSize: (kwh: number, kw: number) => void;
}) {
  const { t } = useTranslation();
  const [kwh, setKwh] = useState(bat.capacity_kwh);
  const [kw, setKw] = useState(bat.power_kw);
  useEffect(() => { setKwh(bat.capacity_kwh); setKw(bat.power_kw); },
            [bat.capacity_kwh, bat.power_kw]);
  if (!free) {
    const cur = `${bat.capacity_kwh}|${bat.power_kw}`;
    const known = HOME_SIZES.some(([c, p]) => `${c}|${p}` === cur);
    return (
      <select value={cur} style={{ fontSize: "0.72rem" }}
              title={t("bat.sizeTitle")}
              onChange={(e) => { const [c, p] = e.target.value.split("|").map(Number); onSize(c, p); }}>
        {!known && <option value={cur}>{bat.capacity_kwh} kWh · {bat.power_kw} kW</option>}
        {HOME_SIZES.map(([c, p]) => (
          <option key={c} value={`${c}|${p}`}>{c} kWh · {p} kW</option>
        ))}
      </select>
    );
  }
  const dirty = kwh !== bat.capacity_kwh || kw !== bat.power_kw;
  const valid = kwh > 0 && kw > 0;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.72rem" }}
          title={t("bat.sizeTitle")}>
      <input type="number" min={1} step={1} value={kwh} style={numStyle}
             onChange={(e) => setKwh(+e.target.value)} /> kWh
      <input type="number" min={1} step={1} value={kw} style={{ ...numStyle, width: 56 }}
             onChange={(e) => setKw(+e.target.value)} /> kW
      {dirty && (
        <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                disabled={!valid} onClick={() => onSize(kwh, kw)}>
          {t("bat.apply")}
        </button>
      )}
    </span>
  );
}

export function ControllerLimit({ ctrl, onLimit }: {
  ctrl: GridController; onLimit: (pct: number) => void;
}) {
  const { t } = useTranslation();
  const [v, setV] = useState(ctrl.limit_pct);
  useEffect(() => setV(ctrl.limit_pct), [ctrl.limit_pct]);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: "0.72rem" }}
          title={t("ctrl.limitTitle")}>
      {t("ctrl.limit")}
      <input type="number" min={20} max={150} step={5} value={v} style={{ ...numStyle, width: 52 }}
             onChange={(e) => setV(+e.target.value)}
             onBlur={() => v !== ctrl.limit_pct && v >= 20 && v <= 150 && onLimit(v)}
             onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} /> %
    </span>
  );
}

export function RontTarget({ ront, onTarget }: {
  ront: RontInfo; onTarget: (v_target: number) => void;
}) {
  const { t } = useTranslation();
  const [v, setV] = useState(Math.round(ront.v_target * V_BASE * 10) / 10);
  useEffect(() => setV(Math.round(ront.v_target * V_BASE * 10) / 10), [ront.v_target]);
  const commit = () => {
    const pu = v / V_BASE;
    if (pu >= 0.9 && pu <= 1.1 && Math.abs(pu - ront.v_target) > 1e-6) onTarget(pu);
  };
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: "0.72rem" }}
          title={t("ront.targetTitle")}>
      {t("ront.target")}
      <input type="number" min={207} max={253} step={0.5} value={v} style={{ ...numStyle, width: 58 }}
             onChange={(e) => setV(+e.target.value)}
             onBlur={commit}
             onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} /> V
    </span>
  );
}

export function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="stat-row">
      <span className="muted">{label}</span>
      <span className="v" style={color ? { color } : undefined}>
        {value}
      </span>
    </div>
  );
}
