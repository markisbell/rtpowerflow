import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { GridListItem, GridPreview, GridsResponse } from "../types";
import { fmt } from "../scales";

interface Props {
  selected: string | null;
  onSelect: (id: string) => void;
  onContinue: () => void;
}

type Voltage = "MV" | "LV";
type Character = "rural" | "suburban" | "urban";

const CHARACTER_IDS: Character[] = ["rural", "suburban", "urban"];

export default function GridBrowser({ selected, onSelect, onContinue }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<GridsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<GridPreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  const [voltage, setVoltage] = useState<Voltage>("MV");
  const [character, setCharacter] = useState<Character>("suburban");
  const [target, setTarget] = useState(100); // desired node count

  useEffect(() => {
    api.grids().then(setData).catch((e) => setError(String(e)));
  }, []);

  // grids matching the chosen type + character, sorted by node count
  const matches = useMemo(() => {
    if (!data) return [];
    return data.grids
      .filter((g) => g.voltage === voltage && g.character === character)
      .sort((a, b) => (a.nodes ?? 0) - (b.nodes ?? 0));
  }, [data, voltage, character]);

  // the grid whose node count is closest to the target slider
  const best = useMemo(() => {
    if (!matches.length) return null;
    return matches.reduce((p, c) =>
      Math.abs((c.nodes ?? 0) - target) < Math.abs((p.nodes ?? 0) - target) ? c : p);
  }, [matches, target]);

  // auto-select the closest match whenever filters/target change
  useEffect(() => {
    if (best && best.id !== selected) onSelect(best.id);
  }, [best]); // eslint-disable-line react-hooks/exhaustive-deps

  // fetch the preview for the selected grid, ignoring stale/overlapping responses
  const reqRef = useRef(0);
  useEffect(() => {
    if (!selected) return;
    const myReq = ++reqRef.current;
    setLoadingPreview(true);
    setPreview(null);
    api
      .gridPreview(selected)
      .then((p) => { if (reqRef.current === myReq) setPreview(p); })
      .catch((e) => { if (reqRef.current === myReq) setError(String(e)); })
      .finally(() => { if (reqRef.current === myReq) setLoadingPreview(false); });
  }, [selected]);

  if (error) return <div className="empty">{t("grid.failed")}<br />{error}</div>;
  if (!data) return <div className="spinner">{t("grid.loadingLib")}</div>;
  if (!data.available)
    return (
      <div className="empty">
        {t("grid.noLibrary")}
        <br />
        <span className="muted">{t("grid.runScript")}</span>
      </div>
    );

  return (
    <div className="browser">
      <div className="grid-gallery">
        <h2 style={{ marginTop: 0 }}>{t("grid.title")}</h2>
        <p className="muted" style={{ marginTop: "-0.4rem", fontSize: "0.85rem" }}>
          {t("grid.subtitle")}
        </p>

        <div className="gen-controls card">
          <div className="gen-row">
            <label>{t("grid.voltageLevel")}</label>
            <div className="seg">
              {(["MV", "LV"] as Voltage[]).map((v) => (
                <button key={v} className={voltage === v ? "on" : ""} onClick={() => setVoltage(v)}>
                  {v === "MV" ? t("grid.mv") : t("grid.lv")}
                </button>
              ))}
            </div>
          </div>

          <div className="gen-row">
            <label>{t("grid.areaCharacter")}</label>
            <div className="seg">
              {CHARACTER_IDS.map((c) => (
                <button key={c} className={character === c ? "on" : ""}
                        title={t(`grid.${c}Hint`)} onClick={() => setCharacter(c)}>
                  {t(`grid.${c}`)}
                </button>
              ))}
            </div>
          </div>

          <div className="gen-row">
            <label>{t("grid.approxNodes")} <span className="muted">({target})</span></label>
            <input type="range" min={10} max={500} step={5} value={target}
                   onChange={(e) => setTarget(+e.target.value)} />
          </div>
        </div>

        {matches.length === 0 ? (
          <div className="empty" style={{ height: "auto", padding: "2rem 0" }}>
            {t("grid.noMatch", { voltage, character: t(`grid.${character}`) })}
          </div>
        ) : (
          <div className="gallery-grid">
            {matches.map((g) => (
              <GridCard key={g.id} g={g} selected={g.id === selected}
                        onClick={() => { onSelect(g.id); setTarget(g.nodes ?? target); }} />
            ))}
          </div>
        )}
      </div>

      <div className="preview-pane">
        {!selected && <div className="muted">{t("grid.adjust")}</div>}
        {loadingPreview && <div className="spinner">{t("grid.building")}</div>}
        {preview && (
          <>
            <h3 style={{ marginTop: 0 }}>{preview.name}</h3>
            <div className="kpis">
              <Kpi k={t("grid.kBuses")} v={preview.n_bus} />
              <Kpi k={t("grid.kLines")} v={preview.n_line} />
              <Kpi k={t("grid.kTrafos")} v={preview.n_trafo} />
              <Kpi k={t("grid.kLoads")} v={preview.n_load} />
            </div>
            {preview.trafos[0] && (
              <p className="muted" style={{ fontSize: "0.82rem" }}>
                {t("grid.transformer", { kva: fmt(preview.trafos[0].sn_mva * 1000, 0) })}
              </p>
            )}
            {preview.notes.length > 0 && (
              <details>
                <summary className="note">{t("grid.notes", { count: preview.notes.length })}</summary>
                <ul style={{ fontSize: "0.75rem", color: "var(--muted)" }}>
                  {preview.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </details>
            )}
            <button className="primary" style={{ marginTop: "1rem" }} onClick={onContinue}>
              {t("grid.configure")}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function GridCard({ g, selected, onClick }: { g: GridListItem; selected: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <div className={`card grid-card${selected ? " sel" : ""}`} onClick={onClick}>
      <div className="meta">
        <div className="title">{g.name}</div>
        <div className="sub">
          <span className="tag">{g.voltage}</span>
          <span className="tag">{g.character ? t(`grid.${g.character}`) : ""}</span>
          {g.nodes != null && <> {t("grid.nodes", { count: g.nodes })}</>}
        </div>
      </div>
    </div>
  );
}

function Kpi({ k, v }: { k: string; v: number }) {
  return (
    <div className="kpi">
      <div className="v">{v}</div>
      <div className="k">{k}</div>
    </div>
  );
}
