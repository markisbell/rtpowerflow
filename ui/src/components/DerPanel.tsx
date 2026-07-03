import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { NodeDer } from "../types";

const hhmm = (m: number) =>
  `${String(Math.floor((m % 1440) / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

/** Per-node DER configuration inside a bus section: resize the PV system
 *  (kWp) and move the EV charge window (start instant + 1–4 h duration).
 *  Values are derived server-side from the profile rows, so LoadStudio-assigned
 *  and runtime-added systems are equally editable. */
export default function DerPanel({ bus, stamp, onChanged }: {
  bus: number;
  stamp: number;              // bumped by the parent after add-PV/EV via menu
  onChanged: () => void;      // notify parent: graphs + icons need a refresh
}) {
  const { t } = useTranslation();
  const [der, setDer] = useState<NodeDer | null>(null);
  const [kwp, setKwp] = useState(5);
  const [start, setStart] = useState(18 * 60);
  const [dur, setDur] = useState(120);

  useEffect(() => {
    let alive = true;
    api.nodeDer(bus).then((d) => {
      if (!alive) return;
      setDer(d);
      if (d.pv) setKwp(d.pv.kwp);
      if (d.ev) { setStart(d.ev.start_min); setDur(d.ev.dur_min); }
    }).catch(() => alive && setDer(null));
    return () => { alive = false; };
  }, [bus, stamp]);

  if (!der || (!der.pv && !der.ev)) return null;
  const pvDirty = der.pv != null && Math.abs(kwp - der.pv.kwp) > 0.01;
  const evDirty = der.ev != null && (start !== der.ev.start_min || dur !== der.ev.dur_min);

  const applyPv = async () => {
    if (!der.pv) return;
    const d = await api.setPv(der.pv.sgen, kwp);
    setDer(d);
    onChanged();
  };
  const applyEv = async () => {
    if (!der.ev) return;
    const d = await api.setEv(der.ev.load, start, dur);
    setDer(d);
    if (d.ev) { setStart(d.ev.start_min); setDur(d.ev.dur_min); }
    onChanged();
  };

  const row = { display: "flex", alignItems: "center", gap: 6, fontSize: "0.74rem" } as const;
  const num = { fontVariantNumeric: "tabular-nums", minWidth: 52, textAlign: "right" } as const;

  return (
    <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
      {der.pv && (
        <div style={row}>
          <span title={t("der.pvTitle")}>☀️</span>
          <input type="range" min={0.5} max={30} step={0.5} value={kwp}
                 style={{ flex: 1 }} onChange={(e) => setKwp(+e.target.value)} />
          <span className="muted" style={num}>{kwp.toFixed(1)} kWp</span>
          <button className="ghost" style={{ padding: "0 6px", fontSize: "0.7rem" }}
                  disabled={!pvDirty} onClick={applyPv}>{t("der.apply")}</button>
        </div>
      )}
      {der.ev && (
        <>
          <div style={{ ...row, marginTop: der.pv ? 4 : 0 }}>
            <span title={t("der.evTitle", { kw: der.ev.kw })}>🔌</span>
            <span className="muted" style={{ fontSize: "0.68rem" }}>{t("der.start")}</span>
            <input type="range" min={0} max={1425} step={15} value={start}
                   style={{ flex: 1 }} onChange={(e) => setStart(+e.target.value)} />
            <span className="muted" style={num}>{hhmm(start)}</span>
          </div>
          <div style={row}>
            <span style={{ width: 14 }} />
            <span className="muted" style={{ fontSize: "0.68rem" }}>{t("der.dur")}</span>
            <input type="range" min={60} max={240} step={15} value={dur}
                   style={{ flex: 1 }} onChange={(e) => setDur(+e.target.value)} />
            <span className="muted" style={num}>{(dur / 60).toFixed(2).replace(".", ",")} h</span>
            <button className="ghost" style={{ padding: "0 6px", fontSize: "0.7rem" }}
                    disabled={!evDirty} onClick={applyEv}>{t("der.apply")}</button>
          </div>
        </>
      )}
    </div>
  );
}
