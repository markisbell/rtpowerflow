import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { NodeProfiles, NodeSeriesKind } from "../types";
import { voltageColor, V_BASE } from "../scales";
import ProfileGraph, { type GSeries } from "./ProfileGraph";

const COLOR: Record<NodeSeriesKind, string> = {
  residential: "#4c8dff", ev: "#f2ae00", pv: "#3fb950",
  wind: "#39c5cf", biogas: "#b08968", gen: "#8b949e",
};
const VLIMIT = [
  { value: 1.1, label: `${Math.round(1.1 * V_BASE)} V`, color: "#f85149" },
  { value: 0.9, label: `${Math.round(0.9 * V_BASE)} V`, color: "#f85149" },
];

// Per-node daily graph: power (residential/EV/PV load & generation) or voltage,
// switchable, with the EN 50160 ±10 % voltage limits shown in voltage mode.
// `embedded`: rendered inside an accordion Section, which owns title + close.
export default function NodeProfile({ bus, name, now, day, onClose, embedded = false }: { bus: number; name: string; now: number | null; day: number; onClose?: () => void; embedded?: boolean }) {
  const { t } = useTranslation();
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
    label: t(`node.${s.kind}`), color: COLOR[s.kind], data: s.p_mw, fill: true,
  }));
  const hasVoltage = (data?.voltage?.length ?? 0) > 0;

  return (
    <div style={embedded ? {} : { marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
        {!embedded && <span style={{ fontWeight: 600 }}>{t("node.title", { name })}</span>}
        <span style={{ display: "flex", gap: 4, alignItems: "center", marginLeft: "auto" }}>
          <button className={mode === "power" ? "on" : ""} style={{ fontSize: "0.68rem", padding: "1px 6px" }} onClick={() => setMode("power")}>{t("node.power")}</button>
          <button className={mode === "voltage" ? "on" : ""} style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                  onClick={() => setMode("voltage")} disabled={!hasVoltage}>{t("node.voltage")}</button>
          {!embedded && onClose && (
            <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>
          )}
        </span>
      </div>

      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("common.error", { msg: err })}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("common.loading")}</div>}
      {!err && data && mode === "power" && (
        powerSeries.length
          ? <ProfileGraph series={powerSeries} scale={1000} unit="kW" dec={1} now={now} />
          : <div className="muted" style={{ fontSize: "0.72rem" }}>{t("node.none")}</div>
      )}
      {!err && data && mode === "voltage" && hasVoltage && (
        <ProfileGraph
          series={[{ label: t("node.voltageSeries"), color: "#c586ff", data: data.voltage, colorData: data.voltage, colorFn: voltageColor },
                   ...((data.est_voltage?.some((v) => v != null) ?? false)
                     ? [{ label: t("graph.est"), color: "#e879f9", data: data.est_voltage! }] : [])]}
          limits={VLIMIT} scale={V_BASE} unit="V" dec={1} baseZero={false} now={now}
        />
      )}
    </div>
  );
}
