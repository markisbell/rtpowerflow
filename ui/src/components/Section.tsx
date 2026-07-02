import type { ReactNode } from "react";

/** A collapsible side-panel section: chevron + title + equipment badges, and an
 *  optional close (✕) when the section is only pinned (not backed by equipment).
 *  The body renders only while open, so heavy children (daily-profile fetches)
 *  load lazily on first expand. */
export default function Section({ title, badges = [], open, onToggle, onClose, children }: {
  title: string;
  badges?: string[];
  open: boolean;
  onToggle: () => void;
  onClose?: () => void;
  children: ReactNode;
}) {
  return (
    <div className="section">
      <div className="sec-head" onClick={onToggle}>
        <span className={`chev${open ? " open" : ""}`}>▸</span>
        <span className="sec-title">{title}</span>
        <span className="sec-badges">{badges.map((b, i) => <span key={i}>{b}</span>)}</span>
        {onClose && (
          <button className="ghost sec-close" onClick={(e) => { e.stopPropagation(); onClose(); }}>✕</button>
        )}
      </div>
      {open && <div className="sec-body">{children}</div>}
    </div>
  );
}
