import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Scenario } from "../types";
import { gridDisplayName } from "../gridname";

/** Save the current live setup as a named scenario recipe, and load saved
 *  ones back — for prepared education/demo situations. Files live server-side
 *  under data/scenarios/ and are hand-editable JSON. */
export default function ScenarioPanel({ onLoaded }: { onLoaded: () => void }) {
  const { t } = useTranslation();
  const [items, setItems] = useState<Scenario[]>([]);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = () => api.scenarios().then((r) => setItems(r.scenarios)).catch(() => {});
  useEffect(() => { refresh(); }, []);

  const save = async () => {
    if (!name.trim()) return;
    setBusy(true); setErr(null);
    try {
      await api.saveScenario(name.trim(), desc.trim());
      setName(""); setDesc("");
      refresh();
    } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };
  const load = async (id: string) => {
    setBusy(true); setErr(null);
    try {
      await api.loadScenario(id);
      onLoaded();
    } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };
  const remove = async (id: string) => {
    try { await api.deleteScenario(id); } finally { refresh(); }
  };

  return (
    <div>
      {items.length === 0 && (
        <div className="muted" style={{ fontSize: "0.72rem", marginTop: 4 }}>{t("scen.none")}</div>
      )}
      {items.map((sc) => (
        <div key={sc.id} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.74rem", margin: "3px 0" }}
             title={`${sc.description || ""}${sc.grid_id ? `\n${gridDisplayName(sc.grid_id, sc.grid_id, t)}` : ""}`}>
          <span style={{ flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {sc.name}
          </span>
          <button className="primary" style={{ padding: "1px 8px", fontSize: "0.7rem" }}
                  disabled={busy} onClick={() => load(sc.id)}>▶ {t("scen.load")}</button>
          <button className="ghost" style={{ padding: "0 5px", fontSize: "0.7rem" }}
                  title={t("scen.delete")} onClick={() => remove(sc.id)}>✕</button>
        </div>
      ))}

      <div style={{ marginTop: 8, borderTop: "1px solid var(--border)", paddingTop: 6,
                    display: "flex", flexDirection: "column", gap: 4 }}>
        <div className="muted" style={{ fontSize: "0.7rem" }}>{t("scen.saveHint")}</div>
        <input value={name} placeholder={t("scen.name")} maxLength={60}
               style={{ background: "var(--panel-2)", border: "1px solid var(--border)",
                        color: "var(--text)", borderRadius: 5, padding: "3px 6px", fontSize: "0.74rem" }}
               onChange={(e) => setName(e.target.value)} />
        <div style={{ display: "flex", gap: 4 }}>
          <input value={desc} placeholder={t("scen.desc")} maxLength={140}
                 style={{ flex: 1, background: "var(--panel-2)", border: "1px solid var(--border)",
                          color: "var(--text)", borderRadius: 5, padding: "3px 6px", fontSize: "0.74rem" }}
                 onChange={(e) => setDesc(e.target.value)} />
          <button className="ghost" style={{ padding: "1px 8px", fontSize: "0.72rem" }}
                  disabled={busy || !name.trim()} onClick={save}>💾 {t("scen.save")}</button>
        </div>
      </div>
      {err && <div className="note" style={{ fontSize: "0.7rem" }}>{err}</div>}
    </div>
  );
}
