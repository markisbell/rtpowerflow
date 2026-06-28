import { useEffect, useState } from "react";
import { api } from "../api";
import type { GridListItem, GridPreview, GridsResponse } from "../types";
import { fmt } from "../scales";

interface Props {
  selected: string | null;
  onSelect: (id: string) => void;
  onContinue: () => void;
}

export default function GridBrowser({ selected, onSelect, onContinue }: Props) {
  const [data, setData] = useState<GridsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<GridPreview | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);

  useEffect(() => {
    api.grids().then(setData).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!selected) return;
    setLoadingPreview(true);
    setPreview(null);
    api
      .gridPreview(selected)
      .then(setPreview)
      .catch((e) => setError(String(e)))
      .finally(() => setLoadingPreview(false));
  }, [selected]);

  if (error) return <div className="empty">Failed to load grids:<br />{error}</div>;
  if (!data) return <div className="spinner">Loading grid catalog…</div>;
  if (!data.available)
    return (
      <div className="empty">
        No grid archive found on the server.
        <br />
        <span className="muted">Expected: {data.archive}</span>
      </div>
    );

  return (
    <div className="browser">
      <div className="grid-gallery">
        <h2 style={{ marginTop: 0 }}>
          Choose a grid <span className="muted">({data.grids.length} low-voltage networks)</span>
        </h2>
        <div className="gallery-grid">
          {data.grids.map((g) => (
            <GridCard key={g.id} g={g} selected={g.id === selected} onClick={() => onSelect(g.id)} />
          ))}
        </div>
      </div>

      <div className="preview-pane">
        {!selected && <div className="muted">Select a grid to preview it.</div>}
        {loadingPreview && <div className="spinner">Converting workbook…</div>}
        {preview && (
          <>
            <h3 style={{ marginTop: 0 }}>{preview.name}</h3>
            <div className="kpis">
              <Kpi k="buses" v={preview.n_bus} />
              <Kpi k="lines" v={preview.n_line} />
              <Kpi k="trafos" v={preview.n_trafo} />
              <Kpi k="loads" v={preview.n_load} />
            </div>
            {preview.trafos[0] && (
              <p className="muted" style={{ fontSize: "0.82rem" }}>
                Transformer: {fmt(preview.trafos[0].sn_mva * 1000, 0)} kVA
              </p>
            )}
            {preview.notes.length > 0 && (
              <details>
                <summary className="note">{preview.notes.length} import note(s)</summary>
                <ul style={{ fontSize: "0.75rem", color: "var(--muted)" }}>
                  {preview.notes.map((n, i) => (
                    <li key={i}>{n}</li>
                  ))}
                </ul>
              </details>
            )}
            <button className="primary" style={{ marginTop: "1rem" }} onClick={onContinue}>
              Configure loads →
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function GridCard({ g, selected, onClick }: { g: GridListItem; selected: boolean; onClick: () => void }) {
  return (
    <div className={`card grid-card${selected ? " sel" : ""}`} onClick={onClick}>
      <div
        className="thumb"
        style={g.thumbnail ? { backgroundImage: `url(${api.thumbnailUrl(g.id)})` } : undefined}
      />
      <div className="meta">
        <div className="title">{g.name}</div>
        <div className="sub">
          <span className="tag">{g.category}</span>
          {g.n_bus != null && <> {g.n_bus} bus</>}
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
