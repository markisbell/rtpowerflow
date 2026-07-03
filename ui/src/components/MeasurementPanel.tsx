import { useTranslation } from "react-i18next";
import type { MeasurementsResponse, MeterPreset } from "../types";

/** Bulk measurement tools: coverage bar + placement presets. Individual meters
 *  are placed/removed through each element's action menu, and their readings
 *  live in that element's side-panel section. */
export default function MeasurementPanel({ placement, onPreset }: {
  placement: MeasurementsResponse;
  onPreset: (name: MeterPreset) => void;
}) {
  const { t } = useTranslation();
  const { node_buses, trafo_idxs, coverage } = placement;
  const nPlaced = node_buses.length + trafo_idxs.length;

  return (
    <div>
      <div className="muted" style={{ fontSize: "0.7rem", fontVariantNumeric: "tabular-nums", marginTop: 4 }}>
        {t("meas.coverageVal", {
          nodes: coverage.n_node_meter, totalNodes: coverage.n_bus,
          trafos: coverage.n_trafo_meter, totalTrafos: coverage.n_trafo,
        })}
      </div>
      <div style={{ height: 5, background: "#1b2028", borderRadius: 3, overflow: "hidden", margin: "4px 0 6px" }}>
        <span style={{ display: "block", height: "100%", background: "#4c8dff",
                       width: `${Math.round((coverage.node_fraction || 0) * 100)}%` }} />
      </div>
      {nPlaced === 0 && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("meas.none")}</div>}

      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 7 }}>
        <button className="ghost" style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                onClick={() => onPreset("all_nodes")}>{t("meas.presetAllNodes")}</button>
        {coverage.n_trafo > 0 && (
          <>
            <button className="ghost" style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                    onClick={() => onPreset("substation_trafos")}>{t("meas.presetSubstations")}</button>
            <button className="ghost" style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                    onClick={() => onPreset("all_trafos")}>{t("meas.presetAllTrafos")}</button>
          </>
        )}
        {nPlaced > 0 && (
          <button className="ghost" style={{ fontSize: "0.68rem", padding: "1px 6px" }}
                  onClick={() => onPreset("clear")}>{t("meas.clear")}</button>
        )}
      </div>
      {!placement.expose_ground_truth && (
        <div className="muted" style={{ fontSize: "0.68rem", marginTop: 5 }}>{t("meas.strict")}</div>
      )}
    </div>
  );
}
