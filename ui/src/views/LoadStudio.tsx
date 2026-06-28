import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { ActiveGrid, Archetype, AssignResponse, LoadgenPolicy } from "../types";
import { fmt } from "../scales";
import Sparkline from "../components/Sparkline";

interface Props {
  gridId: string;
  onApplied: (active: ActiveGrid) => void;
}

export default function LoadStudio({ gridId, onApplied }: Props) {
  const [archetypes, setArchetypes] = useState<Archetype[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<"round_robin" | "random">("round_robin");
  const [seed, setSeed] = useState(0);
  const [scale, setScale] = useState(1);
  const [pf, setPf] = useState(0.95);
  const [evPen, setEvPen] = useState(0);
  const [evKw, setEvKw] = useState(11);
  const [pvPen, setPvPen] = useState(0);
  const [pvKwp, setPvKwp] = useState(5);
  const [preview, setPreview] = useState<AssignResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.archetypes().then((r) => {
      setAvailable(r.available);
      setArchetypes(r.archetypes);
      setChosen(new Set(r.archetypes.map((a) => a.id)));
    });
  }, []);

  const policy = useMemo<LoadgenPolicy>(
    () => ({
      archetypes: chosen.size ? [...chosen] : null,
      mode,
      seed,
      scale,
      power_factor: pf,
      ev_penetration: evPen,
      ev_charger_kw: evKw,
      pv_penetration: pvPen,
      pv_kwp: pvKwp,
    }),
    [chosen, mode, seed, scale, pf, evPen, evKw, pvPen, pvKwp],
  );

  const toggle = (id: string) =>
    setChosen((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  const doPreview = () => {
    setBusy(true);
    setError(null);
    api
      .assign(gridId, policy)
      .then(setPreview)
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  const apply = (withLoadgen: boolean) => {
    setBusy(true);
    setError(null);
    api
      .apply(gridId, withLoadgen ? policy : undefined)
      .then((r) => onApplied(r.active))
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  const netKw = preview ? preview.net_p_mw.map((p) => p * 1000) : [];
  const grossKw = preview ? preview.total_load_p_mw.map((p) => p * 1000) : [];
  const hasPv = !!preview && preview.n_pv > 0;

  return (
    <div className="studio">
      <div className="controls">
        <h2 style={{ marginTop: 0 }}>Generate loads</h2>
        <p className="muted" style={{ fontSize: "0.82rem" }}>
          Grid <b>{gridId}</b>. Profiles are real LPG households assigned across the loads.
        </p>

        {!available && (
          <p className="note">
            No LPG library on the server — you can still run with synthetic placeholder loads.
          </p>
        )}

        {archetypes && available && (
          <div className="field">
            <label>Household archetypes ({chosen.size} selected)</label>
            <div className="arch-list">
              {archetypes.map((a) => (
                <label className="arch-row" key={a.id}>
                  <input type="checkbox" checked={chosen.has(a.id)} onChange={() => toggle(a.id)} />
                  <span style={{ flex: 1 }}>{a.label}</span>
                  <span className="muted">{fmt(a.annual_kwh, 0)} kWh/yr</span>
                </label>
              ))}
            </div>
          </div>
        )}

        <div className="field">
          <label>Assignment mode</label>
          <select value={mode} onChange={(e) => setMode(e.target.value as "round_robin" | "random")}>
            <option value="round_robin">Round-robin (even spread)</option>
            <option value="random">Random (seeded)</option>
          </select>
        </div>
        <div className="field">
          <label>Random seed</label>
          <input type="number" value={seed} onChange={(e) => setSeed(+e.target.value)} />
        </div>
        <div className="field">
          <label>Scale factor ({scale.toFixed(2)}×)</label>
          <input type="range" min={0.2} max={3} step={0.1} value={scale} onChange={(e) => setScale(+e.target.value)} />
        </div>
        <div className="field">
          <label>Power factor ({pf.toFixed(2)})</label>
          <input type="range" min={0.8} max={1} step={0.01} value={pf} onChange={(e) => setPf(+e.target.value)} />
        </div>

        <div className="field">
          <label>🔌 EV penetration ({Math.round(evPen * 100)}%)</label>
          <input type="range" min={0} max={1} step={0.05} value={evPen}
                 onChange={(e) => setEvPen(+e.target.value)} />
          <span className="muted" style={{ fontSize: "0.72rem" }}>
            Share of homes charging an EV in the evening (uncontrolled).
          </span>
        </div>
        {evPen > 0 && (
          <div className="field">
            <label>Wallbox power</label>
            <select value={evKw} onChange={(e) => setEvKw(+e.target.value)}>
              <option value={3.7}>3.7 kW (slow)</option>
              <option value={11}>11 kW (wallbox)</option>
              <option value={22}>22 kW (fast)</option>
            </select>
          </div>
        )}
        <div className="field">
          <label>☀️ PV penetration ({Math.round(pvPen * 100)}%)</label>
          <input type="range" min={0} max={1} step={0.05} value={pvPen}
                 onChange={(e) => setPvPen(+e.target.value)} />
        </div>
        {pvPen > 0 && (
          <div className="field">
            <label>PV size per home ({pvKwp.toFixed(1)} kWp)</label>
            <input type="range" min={1} max={15} step={0.5} value={pvKwp}
                   onChange={(e) => setPvKwp(+e.target.value)} />
          </div>
        )}

        <button className="ghost" onClick={doPreview} disabled={busy || !available}>
          {busy ? "…" : "Preview"}
        </button>
        <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.8rem" }}>
          <button className="primary" onClick={() => apply(available)} disabled={busy}>
            Apply &amp; run →
          </button>
          {available && (
            <button className="ghost" onClick={() => apply(false)} disabled={busy} title="Skip LPG, use placeholder loads">
              Placeholder
            </button>
          )}
        </div>
        {error && <p className="note">{error}</p>}
      </div>

      <div className="preview">
        <h3 style={{ marginTop: 0 }}>Aggregate daily load</h3>
        {!preview && <div className="muted">Click “Preview” to simulate the feeder’s net load curve.</div>}
        {preview && (
          <>
            <div className="kpis">
              <Kpi k="loads" v={`${preview.n_load}`} />
              <Kpi k="EVs" v={`${preview.n_ev}`} />
              <Kpi k="PV systems" v={`${preview.n_pv}`} />
              <Kpi k="net peak" v={`${fmt(preview.peak_net_mw * 1000, 1)} kW`} />
              <Kpi
                k="min net"
                v={`${fmt(preview.min_net_mw * 1000, 1)} kW`}
              />
            </div>
            <Sparkline
              values={netKw}
              overlay={hasPv ? grossKw : undefined}
              width={640}
              height={240}
            />
            <p className="muted" style={{ fontSize: "0.78rem" }}>
              Net feeder demand (solid) over 24 h, kW{hasPv && " — dashed = gross load before PV"}.
              {preview.min_net_mw < 0 && (
                <span className="note"> Net goes negative → midday reverse flow (PV export).</span>
              )}
            </p>
            <p className="muted" style={{ fontSize: "0.78rem" }}>
              Archetypes used: {preview.archetypes_used.join(", ")}.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

function Kpi({ k, v }: { k: string; v: string }) {
  return (
    <div className="kpi">
      <div className="v">{v}</div>
      <div className="k">{k}</div>
    </div>
  );
}
