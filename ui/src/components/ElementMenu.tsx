import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import type { BatteryMode } from "../types";

export interface MenuTarget {
  kind: "bus" | "line" | "trafo";
  id: number;
  name: string;      // element display name for the menu header
  x: number;         // viewport coordinates of the click
  y: number;
}

/** Context menu on a clicked grid element: view its daily profile, add/remove a
 *  battery (per strategy; a transformer's battery sits at its LV busbar) and
 *  place/remove a measurement. Lines only offer the profile. */
export default function ElementMenu({
  target, hasBattery, hasMeter, modes,
  onGraph, onAddBattery, onRemoveBattery, onPlaceMeter, onRemoveMeter, onClose,
}: {
  target: MenuTarget;
  hasBattery: boolean;
  hasMeter: boolean;
  modes: BatteryMode[];
  onGraph: () => void;
  onAddBattery: (mode: BatteryMode) => void;
  onRemoveBattery: () => void;
  onPlaceMeter: () => void;
  onRemoveMeter: () => void;
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
          : (
            <>
              <div className="menu-group">
                {target.kind === "trafo" ? t("menu.addBatteryTrafo") : t("menu.addBattery")}
              </div>
              {modes.map((m) => item(`🔋 ${t(`bat.${m}`)}`, () => onAddBattery(m)))}
            </>
          ))}
      </div>
    </>
  );
}
