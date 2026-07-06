import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { LineProfiles } from "../types";
import { loadingColor } from "../scales";
import ProfileGraph from "./ProfileGraph";

// Per-line daily current graph with the line's rated current (ampacity) limit.
// `embedded`: rendered inside an accordion Section, which owns title + close.
export default function LineProfile({ line, name, now, day, onClose, embedded = false }: { line: number; name: string; now: number | null; day: number; onClose?: () => void; embedded?: boolean }) {
  const { t } = useTranslation();
  const [data, setData] = useState<LineProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null);
    api.lineProfiles(line).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [line, day]);

  const hasData = (data?.current?.length ?? 0) > 0 && data!.current.some((v) => v != null);
  const hasEst = (data?.est_current?.length ?? 0) > 0 && data!.est_current!.some((v) => v != null);

  return (
    <div style={embedded ? {} : { marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      {!embedded && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.78rem", marginBottom: 3 }}>
          <span style={{ fontWeight: 600 }}>{t("line.title", { name })}</span>
          {onClose && <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>}
        </div>
      )}
      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("common.error", { msg: err })}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("common.loading")}</div>}
      {!err && data && hasData && (
        <ProfileGraph
          series={[{ label: t("line.current"), color: "#4c8dff", data: data.current, fill: true,
                     colorData: data.loading, colorFn: loadingColor },
                   ...(hasEst ? [{ label: t("graph.est"), color: "#e879f9", data: data.est_current! }] : [])]}
          limits={data.rated_i_ka != null
            ? [{ value: data.rated_i_ka, label: t("line.rated", { a: (data.rated_i_ka * 1000).toFixed(0) }), color: "#f85149" }]
            : []}
          scale={1000} unit="A" dec={0} now={now} yTitle={t("axis.current")}
        />
      )}
      {!err && data && !hasData && (
        <div className="muted" style={{ fontSize: "0.72rem" }}>{t("line.none")}</div>
      )}
    </div>
  );
}
