import { useTranslation } from "react-i18next";
import type { MeasurementsResponse, MeterPreset, NodeMeasurement, TrafoMeasurement } from "../types";
import { fmt } from "../scales";

// Place / remove measurement devices and show live readings. A grid starts fully
// unobservable; each placed device reveals exactly one node's or transformer's
// quantities. Mirrors BatteryPanel's "select an element, then add here" flow.
export default function MeasurementPanel({
  placement, addBus, addTrafo, liveNodes, liveTrafos,
  onPlaceNode, onRemoveNode, onPlaceTrafo, onRemoveTrafo, onPreset,
}: {
  placement: MeasurementsResponse | null;
  addBus: number | null;
  addTrafo: number | null;
  liveNodes: Map<number, NodeMeasurement>;
  liveTrafos: Map<number, TrafoMeasurement>;
  onPlaceNode: (bus: number) => void;
  onRemoveNode: (bus: number) => void;
  onPlaceTrafo: (trafo: number) => void;
  onRemoveTrafo: (trafo: number) => void;
  onPreset: (name: MeterPreset) => void;
}) {
  const { t } = useTranslation();
  if (!placement) return null;
  const { node_buses, trafo_idxs, coverage } = placement;
  const nPlaced = node_buses.length + trafo_idxs.length;

  return (
    <div style={{ marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <span style={{ fontWeight: 600, fontSize: "0.82rem" }}>{t("meas.heading")}</span>
        <span className="muted" style={{ fontSize: "0.7rem", fontVariantNumeric: "tabular-nums" }}>
          {t("meas.coverageVal", {
            nodes: coverage.n_node_meter, totalNodes: coverage.n_bus,
            trafos: coverage.n_trafo_meter, totalTrafos: coverage.n_trafo,
          })}
        </span>
      </div>

      {/* coverage bar (node meters) */}
      <div style={{ height: 5, background: "#1b2028", borderRadius: 3, overflow: "hidden", margin: "4px 0 6px" }}>
        <span style={{ display: "block", height: "100%", background: "#4c8dff",
                       width: `${Math.round((coverage.node_fraction || 0) * 100)}%` }} />
      </div>

      {/* placed node meters */}
      {node_buses.map((bus) => {
        const r = liveNodes.get(bus);
        return (
          <MeterRow key={`n${bus}`} kind="node" label={t("meas.node", { bus })}
                    reading={r ? `${fmt(r.vm_pu, 3)} pu · ${fmt((r.p_mw ?? 0) * 1000, 1)} kW` : "—"}
                    onRemove={() => onRemoveNode(bus)} removeTitle={t("meas.remove")} />
        );
      })}
      {/* placed transformer meters */}
      {trafo_idxs.map((tr) => {
        const r = liveTrafos.get(tr);
        return (
          <MeterRow key={`t${tr}`} kind="trafo" label={t("meas.trafo", { trafo: tr })}
                    reading={r ? `${fmt(r.loading_percent, 1)} %` : "—"}
                    onRemove={() => onRemoveTrafo(tr)} removeTitle={t("meas.remove")} />
        );
      })}
      {nPlaced === 0 && <div className="muted" style={{ fontSize: "0.72rem" }}>{t("meas.none")}</div>}

      {/* add at the current selection */}
      {addBus == null && addTrafo == null ? (
        <p className="muted" style={{ fontSize: "0.72rem", marginTop: 5 }}>{t("meas.addNodeHint")}</p>
      ) : (
        <div style={{ marginTop: 6 }}>
          {addBus != null && !node_buses.includes(addBus) && (
            <button className="primary" style={{ padding: "2px 8px", fontSize: "0.72rem", marginRight: 5 }}
                    onClick={() => onPlaceNode(addBus)}>{t("meas.addNode", { bus: addBus })}</button>
          )}
          {addTrafo != null && !trafo_idxs.includes(addTrafo) && (
            <button className="primary" style={{ padding: "2px 8px", fontSize: "0.72rem" }}
                    onClick={() => onPlaceTrafo(addTrafo)}>{t("meas.addTrafo", { trafo: addTrafo })}</button>
          )}
        </div>
      )}

      {/* bulk presets */}
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
    </div>
  );
}

function MeterRow({ kind, label, reading, onRemove, removeTitle }: {
  kind: "node" | "trafo"; label: string; reading: string; onRemove: () => void; removeTitle: string;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.72rem", margin: "2px 0" }}>
      <span style={{ flex: "0 0 auto" }}>{kind === "node" ? "📟" : "🔌"}</span>
      <span style={{ flex: "0 0 auto" }}>{label}</span>
      <span className="muted" style={{ flex: 1, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{reading}</span>
      <button className="ghost" style={{ padding: "0 5px", fontSize: "0.7rem" }} title={removeTitle}
              onClick={onRemove}>✕</button>
    </div>
  );
}
