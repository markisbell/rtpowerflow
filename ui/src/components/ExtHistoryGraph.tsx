import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import ProfileGraph from "./ProfileGraph";

/** The external node's received-value day ring (applied kW per step). An
 *  external node has no forecast — its "day graph" is what actually arrived,
 *  so this polls while the section is open (the ring fills as the engine
 *  ticks) instead of loading once like the profile sweeps. */
export default function ExtHistoryGraph({ id, now }: { id: number; now: number | null }) {
  const { t } = useTranslation();
  const [data, setData] = useState<(number | null)[] | null>(null);

  useEffect(() => {
    let live = true;
    const load = () => api.extHistory(id)
      .then((h) => { if (live) setData(h.p_kw); }).catch(() => {});
    load();
    const tmr = setInterval(load, 5000);
    return () => { live = false; clearInterval(tmr); };
  }, [id]);

  if (!data || data.every((v) => v == null)) return null;
  return (
    <div style={{ marginTop: 4 }}>
      <ProfileGraph series={[{ label: t("ext.history"), color: "#b07aff", data }]}
                    scale={1} unit="kW" dec={1} now={now} yTitle={t("axis.power")} />
    </div>
  );
}
