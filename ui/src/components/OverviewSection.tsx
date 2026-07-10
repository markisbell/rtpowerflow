/**
 * The Übersicht side-panel section (extracted from LivePowerFlow, 2026-07-10):
 * per view mode it aggregates a different data layer — the WLS estimate, the
 * revealed ground truth, or only what the placed meters deliver.
 */
import { useTranslation } from "react-i18next";
import Section from "./Section";
import { Stat } from "./EquipmentControls";
import { fmt, loadingColor, V_BASE } from "../scales";
import type { EstimatedState, ObservedSummary, StepSummary } from "../types";

export default function OverviewSection({ open, onToggle, mode, est, summary, observed, solveMs, canReveal }: {
  open: boolean;
  onToggle: () => void;
  mode: "truth" | "observed" | "est";
  est: EstimatedState | null;
  summary: StepSummary | undefined;
  observed: ObservedSummary | null | undefined;
  solveMs: number | null;
  canReveal: boolean;
}) {
  const { t } = useTranslation();
  const reveal = mode === "truth";
  const s = summary;
  const os = observed;
  return (
    <Section title={t("sec.overview")} open={open} onToggle={onToggle}>
      {mode === "est" && est ? (
        // the operator's calculated view: aggregates over the WLS estimate
        <>
          <div className="muted" style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 2 }}>
            🧮 {t("live.estCaption")}
          </div>
          <Stat label={t("live.vminmax")}
                value={(() => { const v = est.buses.map((b) => b.vm_pu).filter((x): x is number => x != null);
                                return v.length ? `${fmt(Math.min(...v) * V_BASE, 1)} / ${fmt(Math.max(...v) * V_BASE, 1)} V` : t("live.na"); })()} />
          <Stat label={t("live.maxLine")}
                value={(() => { const v = est.lines.map((l) => l.loading_percent).filter((x): x is number => x != null);
                                return v.length ? `${fmt(Math.max(...v), 1)} %` : t("live.na"); })()}
                color={loadingColor(Math.max(0, ...est.lines.map((l) => l.loading_percent ?? 0)))} />
          <Stat label={t("live.maxTrafo")}
                value={(() => { const v = est.trafos.map((tr) => tr.loading_percent).filter((x): x is number => x != null);
                                return v.length ? `${fmt(Math.max(...v), 1)} %` : t("live.na"); })()}
                color={loadingColor(Math.max(0, ...est.trafos.map((tr) => tr.loading_percent ?? 0)))} />
          {est.error?.max_dv_pu != null && (
            <Stat label={t("live.estErrV")} value={`${fmt(est.error.max_dv_pu * V_BASE, 2)} V`} />
          )}
          <Stat label={t("live.estSolve")} value={`${fmt(est.solve_ms, 0)} ms`} />
        </>
      ) : reveal && s ? (
        // ground truth (revealed): the true system-wide summary
        <>
          <div className="muted" style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 2 }}>
            👁 {t("live.groundTruth")}
          </div>
          <Stat label={t("live.vminmax")} value={`${fmt(s.vm_pu_min * V_BASE, 1)} / ${fmt(s.vm_pu_max * V_BASE, 1)} V`} />
          <Stat label={t("live.maxLine")} value={`${fmt(s.max_line_loading_percent, 1)} %`} color={loadingColor(s.max_line_loading_percent)} />
          <Stat label={t("live.maxTrafo")} value={s.max_trafo_loading_percent != null ? `${fmt(s.max_trafo_loading_percent, 1)} %` : t("live.na")} color={loadingColor(s.max_trafo_loading_percent)} />
          <Stat label={t("live.totalLoad")} value={`${fmt(s.total_load_mw * 1000, 1)} kW`} />
          <Stat label={t("live.generation")} value={`${fmt(s.total_gen_mw * 1000, 1)} kW`} />
          <Stat label={t("live.slack")} value={`${fmt(s.total_ext_grid_mw * 1000, 1)} kW`} />
          <Stat label={t("live.losses")} value={`${fmt(s.total_losses_mw * 1000, 2)} kW`} />
        </>
      ) : (
        // observed only: aggregates over placed meters
        <>
          <Stat label={t("live.measuredVmm")}
                value={os?.vm_pu_min != null && os?.vm_pu_max != null ? `${fmt(os.vm_pu_min * V_BASE, 1)} / ${fmt(os.vm_pu_max * V_BASE, 1)} V` : t("live.na")} />
          <Stat label={t("live.measuredTrafo")}
                value={os?.max_trafo_loading_percent != null ? `${fmt(os.max_trafo_loading_percent, 1)} %` : t("live.na")}
                color={loadingColor(os?.max_trafo_loading_percent)} />
          <Stat label={t("live.measuredLoad")}
                value={os?.measured_node_p_mw != null ? `${fmt(os.measured_node_p_mw * 1000, 1)} kW` : t("live.na")} />
          <Stat label={t("live.coverage")}
                value={os ? `${os.n_node_meter}/${os.n_bus} · ${os.n_trafo_meter}/${os.n_trafo}` : "—"} />
        </>
      )}
      <Stat label={t("live.solveTime")} value={solveMs != null ? `${fmt(solveMs, 1)} ms` : "—"} />
      {!reveal && (
        <div className="muted" style={{ fontSize: "0.68rem", marginTop: 4 }}>
          {t("live.observedNote")}{!canReveal ? ` ${t("live.truthHidden")}` : ""}
        </div>
      )}
    </Section>
  );
}
