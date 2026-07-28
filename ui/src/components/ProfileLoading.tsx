import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

// The day graphs are not a cheap lookup: the backend solves a WHOLE day of
// power flows (and, in the Schätzung view, runs the WLS at the estimate
// raster) the first time a day is asked for. On a district that is tens of
// seconds — long enough that a bare "lädt…" reads as "nothing happens".
// So after a moment we say what is being computed and count the seconds.
export default function ProfileLoading() {
  const { t } = useTranslation();
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    const h = window.setInterval(() => setSecs((s) => s + 1), 1000);
    return () => window.clearInterval(h);
  }, []);
  return (
    <div className="muted" style={{ fontSize: "0.72rem" }}>
      {secs < 2 ? t("common.loading") : t("common.sweeping", { s: secs })}
    </div>
  );
}
