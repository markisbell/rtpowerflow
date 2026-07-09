import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type EstimationConfig } from "../api";

/** Content of the "Schätzung" menu (hosted by MenuBar): configures what the
 *  WLS state estimation may use. Defaults mirror real DSO practice — no
 *  PV / EV pseudo-measurements. Every change POSTs immediately; the
 *  estimator rebuilds its profile knowledge on the next solved step. */
export function EstimationPanel() {
  const { t } = useTranslation();
  const [cfg, setCfg] = useState<EstimationConfig | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.estConfig().then(setCfg).catch((e) => setErr(String(e)));
  }, []);

  const apply = (patch: Partial<EstimationConfig>) => {
    if (!cfg) return;
    const next = { ...cfg, ...patch };
    setCfg(next);                                  // optimistisch
    setErr(null);
    api.setEstConfig(next).then(setCfg).catch((e) => setErr(String(e)));
  };

  const check = (key: "pv_pseudo" | "ev_pseudo" | "zero_injection", label: string) => (
    <label className="est-row">
      <input type="checkbox" checked={cfg?.[key] ?? false}
             onChange={(e) => apply({ [key]: e.target.checked })} />
      <span>{label}</span>
    </label>
  );

  return (
    <div className="est-body">
      <div className="est-note">{t("est.intro")}</div>

      <div className="est-group">{t("est.pseudoGroup")}</div>
      {check("pv_pseudo", t("est.pvPseudo"))}
      <div className="est-hint">{t("est.pvPseudoHint")}</div>
      {check("ev_pseudo", t("est.evPseudo"))}
      <div className="est-hint">{t("est.evPseudoHint")}</div>
      {check("zero_injection", t("est.zeroInj"))}
      <div className="est-hint">{t("est.zeroInjHint")}</div>

      <div className="est-group">{t("est.basisGroup")}</div>
      <label className="est-row">
        <select value={cfg?.load_basis ?? "profile"}
                onChange={(e) => apply({ load_basis: e.target.value as "profile" | "slp" })}>
          <option value="profile">{t("est.basisProfile")}</option>
          <option value="slp">{t("est.basisSlp")}</option>
        </select>
      </label>
      {cfg?.load_basis === "slp" && (
        <label className="est-row">
          <span>{t("est.slpKwh")}</span>
          <input type="number" min={500} max={20000} step={250}
                 value={cfg.slp_annual_kwh}
                 onChange={(e) => apply({ slp_annual_kwh: +e.target.value })} />
        </label>
      )}
      <div className="est-hint">
        {cfg?.load_basis === "slp" ? t("est.basisSlpHint") : t("est.basisProfileHint")}
      </div>

      <label className="est-row">
        <span>{t("est.stdPct", { pct: Math.round(cfg?.pseudo_std_pct ?? 50) })}</span>
        <input type="range" min={5} max={300} step={5}
               value={cfg?.pseudo_std_pct ?? 50}
               onChange={(e) => apply({ pseudo_std_pct: +e.target.value })} />
      </label>
      <div className="est-hint">{t("est.stdHint")}</div>

      <div className="est-group">{t("est.hierGroup")}</div>
      <label className="est-row">
        <select value={cfg?.hierarchy ?? "auto"}
                onChange={(e) => apply({ hierarchy: e.target.value as EstimationConfig["hierarchy"] })}>
          <option value="auto">{t("est.hierAuto")}</option>
          <option value="hierarchical">{t("est.hierOn")}</option>
          <option value="monolithic">{t("est.hierOff")}</option>
        </select>
      </label>
      <div className="est-hint">{t("est.hierHint")}</div>

      {err && <div className="est-err">{err}</div>}
    </div>
  );
}
