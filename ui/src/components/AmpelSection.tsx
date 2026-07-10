/**
 * The 🚦 Netzampel side-panel section (extracted from LivePowerFlow,
 * 2026-07-10): MV coordinator status (editable limit, EV/PV signal factors)
 * plus the cascade statistics (cells / Steuerboxen / currently dimming).
 */
import { useTranslation } from "react-i18next";
import Section from "./Section";
import { ControllerLimit, Stat } from "./EquipmentControls";
import { fmt } from "../scales";
import type { GridController } from "../types";

export default function AmpelSection({ open, onToggle, coordinator, nCells, nBoxes, nDimming, onLimit, onRemove }: {
  open: boolean;
  onToggle: () => void;
  coordinator: GridController | undefined;   // live frame if available
  nCells: number;
  nBoxes: number;
  nDimming: number;
  onLimit: (cid: number, pct: number) => void;
  onRemove: (cid: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <Section title={`🚦 ${t("ampel.heading")}`} open={open} onToggle={onToggle}
             badges={nDimming ? [`⚡ ${nDimming}`] : []}>
      {coordinator ? (
        <>
          <div className="row" style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ fontWeight: 600 }}>{t("ampel.coordinator")}</span>
            <ControllerLimit ctrl={coordinator}
                             onLimit={(p) => onLimit(coordinator.id, p)} />
            <button className="mini" title={t("ctrl.remove")} style={{ marginLeft: "auto" }}
                    onClick={() => onRemove(coordinator.id)}>✕</button>
          </div>
          <Stat label={t("ctrl.seen")}
                value={coordinator.seen_pct != null
                  ? `${fmt(coordinator.seen_pct, 1)} % (${t(coordinator.seen_src === "meter" ? "ctrl.srcMeter" : "ctrl.srcEst")})`
                  : `⚠️ ${t("ctrl.blind")}`}
                color={coordinator.seen_pct != null && coordinator.seen_pct > coordinator.limit_pct ? "#e05c4a" : undefined} />
          <Stat label={t("ampel.signalEv")} value={`${Math.round(coordinator.ev_factor * 100)} %`}
                color={coordinator.ev_factor < 1 ? "#e0a83a" : undefined} />
          <Stat label={t("ampel.signalPv")} value={`${Math.round(coordinator.pv_factor * 100)} %`}
                color={coordinator.pv_factor < 1 ? "#e0a83a" : undefined} />
        </>
      ) : (
        <div className="muted" style={{ fontSize: "0.78rem" }}>{t("ampel.noCoord")}</div>
      )}
      <Stat label={t("ampel.cells")} value={`${nCells}`} />
      <Stat label={t("ampel.boxes")} value={`${nBoxes}`} />
      <Stat label={t("ampel.dimming")} value={`${nDimming}`}
            color={nDimming ? "#e0a83a" : undefined} />
    </Section>
  );
}
