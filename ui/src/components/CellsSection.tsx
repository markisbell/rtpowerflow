/**
 * The Zellen side-panel section (extracted from LivePowerFlow, 2026-07-10):
 * a scrollable table of all ONS cells with a traffic-light dot (red =
 * station meter shows overload, amber = dimming on coordinator signal,
 * grey = no reading, green = quiet), the live station reading, and the
 * device icons. A click drills the map down into the cell.
 */
import { useTranslation } from "react-i18next";
import Section from "./Section";
import { fmt } from "../scales";
import type { NodeMeasurement, TopoCell, TrafoMeasurement } from "../types";

export default function CellsSection({ open, onToggle, cells, nodeMeas, trafoMeas, signalBuses, boxCells, meterBuses, meterTrafos, focusCell, onBack, onOpen }: {
  open: boolean;
  onToggle: () => void;
  cells: TopoCell[];
  nodeMeas: NodeMeasurement[];       // live frame readings (lumped stations)
  trafoMeas: TrafoMeasurement[];     // live frame readings (station trafos)
  signalBuses: number[];             // stations currently dimming on signal
  boxCells: (string | null | undefined)[];   // cell ids that carry a Steuerbox
  meterBuses: number[];
  meterTrafos: number[];
  focusCell: string | null;
  onBack: () => void;
  onOpen: (c: TopoCell) => void;
}) {
  const { t } = useTranslation();
  const trafoRead = new Map(trafoMeas.map((m) => [m.trafo, m]));
  const nodeRead = new Map(nodeMeas.map((m) => [m.bus, m]));
  const sigSet = new Set(signalBuses);
  const boxSet = new Set(boxCells);
  return (
    <Section title={t("cells.heading")} open={open} onToggle={onToggle}
             badges={[`${cells.length}`]}>
      {focusCell && (
        <button className="ghost" style={{ fontSize: "0.72rem", marginBottom: 4 }}
                onClick={onBack}>← {t("cells.back")}</button>
      )}
      <div style={{ maxHeight: 280, overflowY: "auto", fontSize: "0.74rem" }}>
        {cells.map((c) => {
          const tm = c.station_trafos.length
            ? trafoRead.get(c.station_trafos[0]) : undefined;
          const nm = c.lumped && c.mv_bus != null
            ? nodeRead.get(c.mv_bus) : undefined;
          const stationBus = c.lumped ? c.mv_bus : c.lv_busbar;
          const dimming = stationBus != null && sigSet.has(stationBus);
          const overload = tm?.loading_percent != null && tm.loading_percent > 100;
          const noData = !tm && !nm;
          const dot = overload ? "#f85149" : dimming ? "#f2ae00"
            : noData ? "#8b949e" : "#3fb950";
          const reading = tm?.loading_percent != null
            ? `${fmt(tm.loading_percent, 0)} %`
            : nm?.p_mw != null ? `${fmt(nm.p_mw * 1000, 0)} kW` : "—";
          return (
            <div key={c.id} className="cell-row"
                 onClick={() => onOpen(c)}
                 style={{ display: "flex", alignItems: "center", gap: 6,
                          cursor: "pointer", padding: "1px 2px",
                          background: focusCell === c.id ? "var(--border)" : undefined }}>
              <span style={{ color: dot }}>●</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis",
                             whiteSpace: "nowrap" }}
                    title={c.name}>{c.name.replace(/^lv_/, "")}</span>
              <span className="muted" style={{ fontVariantNumeric: "tabular-nums" }}>{reading}</span>
              <span style={{ width: 30, textAlign: "right" }}>
                {c.station_trafos.some((tId) => meterTrafos.includes(tId))
                  || (c.lumped && c.mv_bus != null && meterBuses.includes(c.mv_bus))
                  ? "📟" : ""}
                {boxSet.has(c.id) ? "🎛" : ""}
              </span>
            </div>
          );
        })}
      </div>
      <div className="muted" style={{ fontSize: "0.68rem", marginTop: 4 }}>
        {t("cells.hint")}
      </div>
    </Section>
  );
}
