import type { TFunction } from "i18next";

/** Localized display name for a library grid. The manifest names are English
 *  ("Rural MV grid · district 3150"); the grid id encodes voltage, character
 *  and district ("mv_rural_3150", "lv_rural_3150_300266"), so the label can be
 *  rebuilt in the current UI language and follows the DE/EN switch. Ids that
 *  don't match the library pattern (e.g. the default sample grid) keep their
 *  original name. */
export function gridDisplayName(id: string | null | undefined, fallback: string, t: TFunction): string {
  const m = id?.match(/^(mv|lv)_(rural|suburban|urban)_(\d+)(?:_(\d+))?$/);
  if (!m) return fallback;
  const adj = t(`grid.${m[2]}Adj`);
  return m[1] === "mv"
    ? t("grid.nameMv", { adj, district: m[3] })
    : t("grid.nameLv", { adj, lvid: m[4] ?? m[3] });
}
