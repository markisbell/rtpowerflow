import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { TrafoProfiles } from "../types";
import { loadingColor } from "../scales";
import ProfileGraph, { type GLimit } from "./ProfileGraph";

// Per-transformer daily power-exchange graph with the transformer's rated
// apparent power as the capacity limit. Plotted is the APPARENT power S in kVA
// (|S| = loading% x rated, signed by the direction of P) so the curve and the
// ±rated limit lines live on the same axis. When the flow reverses (PV export),
// the axis spans ±rated; otherwise it's an import-only 0..rated view.
// `embedded`: rendered inside an accordion Section, which owns title + close.
export default function TrafoProfile({ trafo, name, now, day, onClose, embedded = false }: { trafo: number; name: string; now: number | null; day: number; onClose?: () => void; embedded?: boolean }) {
  const { t } = useTranslation();
  const [data, setData] = useState<TrafoProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.trafoProfiles(trafo).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [trafo, day]);

  const power = data?.power ?? [];
  const sn = data?.sn_mva ?? null;
  // apparent power, signed by the flow direction (falls back to |P| where the
  // loading sample is missing)
  const apparent = power.map((p, i) => {
    if (p == null) return null;
    const l = data?.loading?.[i];
    const s = sn != null && l != null ? (l / 100) * sn : Math.abs(p);
    return p < 0 ? -s : s;
  });
  const hasData = apparent.length > 0 && apparent.some((v) => v != null);
  const hasExport = apparent.some((v) => v != null && v < 0);
  const ratedLabel = sn != null ? t("trafo.rated", { kva: (sn * 1000).toFixed(0) }) : "";
  const limits: GLimit[] = sn == null ? [] :
    hasExport
      ? [{ value: sn, label: ratedLabel, color: "#f85149" },
         { value: -sn, label: t("trafo.ratedNeg", { kva: (sn * 1000).toFixed(0) }), color: "#f85149" }]
      : [{ value: sn, label: ratedLabel, color: "#f85149" }];

  return (
    <div style={embedded ? {} : { marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      {!embedded && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
          <span style={{ fontWeight: 600 }}>{t("trafo.title", { name })}</span>
          {onClose && <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>}
        </div>
      )}
      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("common.error", { msg: err })}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("common.loading")}</div>}
      {!err && data && hasData && (
        <ProfileGraph
          series={[{ label: t("trafo.power"), color: "#f2ae00", data: apparent, fill: !hasExport,
                     colorData: data.loading, colorFn: loadingColor },
                   ...((data.est_power?.some((v) => v != null) ?? false)
                     ? [{ label: t("graph.est"), color: "#e879f9", data: data.est_power! }] : [])]}
          limits={limits} scale={1000} unit="kVA" dec={1} baseZero={!hasExport} now={now}
          yTitle={t("axis.apparent")}
        />
      )}
      {!err && data && !hasData && (
        <div className="muted" style={{ fontSize: "0.72rem" }}>{t("trafo.none")}</div>
      )}
    </div>
  );
}
