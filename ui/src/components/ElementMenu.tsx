import { useEffect } from "react";
import { useTranslation } from "react-i18next";

export interface MenuTarget {
  kind: "bus" | "line" | "trafo";
  id: number;
  name: string;      // element display name for the menu header
  x: number;         // viewport coordinates of the click
  y: number;
}

/** Context menu on a clicked grid element: view its daily profile, add/remove a
 *  battery (strategy is switched later in the node's section; a transformer's
 *  battery sits at its LV busbar) and place/remove a measurement. Lines only
 *  offer the profile. */
export default function ElementMenu({
  target, hasBattery, hasMeter, hasPv, hasEv, hasController, controllerLabel,
  hasRont, onAddRont, onRemoveRont,
  hasExt, onAddExt, onRemoveExt,
  onGraph, onAddBattery, onRemoveBattery, onPlaceMeter, onRemoveMeter,
  onAddPv, onAddEv, onRemovePv, onRemoveEv,
  onAddController, onRemoveController, onClose,
}: {
  target: MenuTarget;
  hasBattery: boolean;
  hasMeter: boolean;
  hasPv: boolean;
  hasEv: boolean;
  hasController: boolean;
  /** overrides the add-controller label (vertical scopes: Steuerbox / Netzampel) */
  controllerLabel?: string;
  /** rONT (on-load tap changer) — transformers only */
  hasRont?: boolean;
  onAddRont?: () => void;
  onRemoveRont?: () => void;
  /** external node (live P/Q feed) — buses only */
  hasExt?: boolean;
  onAddExt?: () => void;
  onRemoveExt?: () => void;
  onGraph: () => void;
  onAddBattery: () => void;
  onRemoveBattery: () => void;
  onPlaceMeter: () => void;
  onRemoveMeter: () => void;
  onAddPv: () => void;
  onAddEv: () => void;
  onRemovePv: () => void;
  onRemoveEv: () => void;
  onAddController: () => void;
  onRemoveController: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  const x = Math.max(4, Math.min(target.x, window.innerWidth - 235));
  const y = Math.max(4, Math.min(target.y, window.innerHeight - 280));
  const item = (label: string, fn: () => void) => (
    <button key={label} className="menu-item" onClick={() => { fn(); onClose(); }}>{label}</button>
  );

  return (
    <>
      <div className="menu-overlay" onClick={onClose}
           onContextMenu={(e) => { e.preventDefault(); onClose(); }} />
      <div className="el-menu" style={{ left: x, top: y }}>
        <div className="menu-title">{target.name}</div>
        {item(`📈 ${t("menu.graph")}`, onGraph)}
        {target.kind !== "line" && (hasMeter
          ? item(`📟 ${t("menu.removeMeter")}`, onRemoveMeter)
          : item(`📟 ${t("menu.addMeter")}`, onPlaceMeter))}
        {target.kind !== "line" && (hasBattery
          ? item(`🔋 ${t("menu.removeBattery")}`, onRemoveBattery)
          : item(`🔋 ${target.kind === "trafo" ? t("menu.addBatteryTrafo") : t("menu.addBattery")}`,
                 onAddBattery))}
        {target.kind !== "line" && (hasController
          ? item(`🎛️ ${t("menu.removeController")}`, onRemoveController)
          : item(`🎛️ ${controllerLabel
                       ?? (target.kind === "trafo" ? t("menu.addControllerTrafo") : t("menu.addController"))}`,
                 onAddController))}
        {target.kind === "trafo" && onAddRont && (hasRont
          ? item(`🔧 ${t("menu.removeRont")}`, onRemoveRont ?? (() => {}))
          : item(`🔧 ${t("menu.addRont")}`, onAddRont))}
        {target.kind === "bus" && (hasPv
          ? item(`☀️ ${t("menu.removePv")}`, onRemovePv)
          : item(`☀️ ${t("menu.addPv")}`, onAddPv))}
        {target.kind === "bus" && (hasEv
          ? item(`🔌 ${t("menu.removeEv")}`, onRemoveEv)
          : item(`🔌 ${t("menu.addEv")}`, onAddEv))}
        {target.kind === "bus" && onAddExt && (hasExt
          ? item(`📡 ${t("menu.removeExt")}`, onRemoveExt ?? (() => {}))
          : item(`📡 ${t("menu.addExt")}`, onAddExt))}
      </div>
    </>
  );
}
