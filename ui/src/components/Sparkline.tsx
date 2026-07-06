interface Props {
  values: number[];
  overlay?: number[]; // optional dashed second series (e.g. gross load vs net)
  width?: number;
  height?: number;
  color?: string;
  fill?: string;
  overlayColor?: string;
  hourAxis?: boolean;
  marker?: number;    // horizontal limit line at ±marker (e.g. trafo rating)
  step?: boolean;     // sample-and-hold steps instead of linear interpolation
  fluid?: boolean;    // scale to the container width (width/height = viewBox)
  xTitle?: string;    // axis caption, e.g. "Zeit / h"
  yTitle?: string;    // axis caption, e.g. "Leistung / kW"
}

/** Y-axis extent of the chart: all plotted series plus headroom for the
 *  marker line. Pure — must NEVER mutate the series (a marker pushed into the
 *  data array would be drawn as a phantom peak at the end of the day). */
export function chartExtent(
  main: number[], over: number[] | null, marker?: number,
): { min: number; max: number } {
  const ext = over ? [...main, ...over] : [...main];
  if (marker && marker > 0) ext.push(marker * 1.05);   // keep the limit in view
  return { max: Math.max(...ext, 1e-9), min: Math.min(...ext, 0) };
}

/** n evenly distributed axis tick values from min to max (both included). */
export function axisTicks(min: number, max: number, n = 5): number[] {
  const span = max - min;
  return Array.from({ length: n }, (_, k) => min + (span * k) / (n - 1));
}

/** A compact area/line chart for a daily profile. Handles negative values
 *  (e.g. net load going negative under high PV) with a zero baseline. */
export default function Sparkline({
  values,
  overlay,
  width = 560,
  height = 200,
  color = "#7fd1ff",
  fill = "#7fd1ff22",
  overlayColor = "#8a93a3",
  hourAxis = true,
  marker,
  step = false,
  fluid = false,
  xTitle,
  yTitle,
}: Props) {
  if (!values.length) return null;
  const FS = 11;                          // one font size for all axis text
  const pad = 26;
  const padL = yTitle ? 56 : 40;          // room for tick labels (+ rotated title)
  const padB = xTitle ? 42 : 26;          // room for tick labels (+ axis title)
  const w = width - padL - pad;
  const h = height - pad - padB;

  const ds = (arr: number[]) => {
    const stride = Math.max(1, Math.floor(arr.length / 280));
    const out: number[] = [];
    for (let i = 0; i < arr.length; i += stride) out.push(arr[i]);
    return out;
  };
  const main = ds(values);
  const over = overlay ? ds(overlay) : null;

  const { min, max } = chartExtent(main, over, marker);
  const span = max - min || 1e-9;

  const x = (i: number, n: number) => padL + (i / (n - 1)) * w;
  const y = (v: number) => pad + h - ((v - min) / span) * h;
  const y0 = y(0);
  const yDec = span < 10 ? 1 : 0;        // tick label decimals
  const yTicks = axisTicks(min, max);

  // step = sample-and-hold: each value holds until the next sample (the natural
  // look for discrete per-minute load profiles); otherwise linear segments
  const path = (arr: number[]) => {
    if (!step) {
      return arr.map((v, i) =>
        `${i === 0 ? "M" : "L"}${x(i, arr.length).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    }
    let d = `M${x(0, arr.length).toFixed(1)},${y(arr[0]).toFixed(1)}`;
    for (let i = 1; i < arr.length; i++) {
      d += ` H${x(i, arr.length).toFixed(1)} V${y(arr[i]).toFixed(1)}`;
    }
    return d;
  };
  const area = `${path(main)} L${x(main.length - 1, main.length).toFixed(1)},${y0} L${padL},${y0} Z`;

  return (
    <svg width={fluid ? undefined : width} height={fluid ? undefined : height}
         viewBox={`0 0 ${width} ${height}`}
         style={fluid ? { width: "100%", height: "auto", display: "block" } : undefined}
         role="img" aria-label="daily load profile">
      {/* grid: a tick + label every 2 h, five evenly spaced y ticks */}
      {hourAxis &&
        Array.from({ length: 13 }, (_, k) => k * 2).map((hr) => (
          <g key={"h" + hr}>
            <line x1={padL + (hr / 24) * w} y1={pad} x2={padL + (hr / 24) * w}
                  y2={pad + h} stroke="#1b2028" strokeWidth={1} />
            <text x={padL + (hr / 24) * w} y={pad + h + FS + 4} fill="#8a93a3"
                  fontSize={FS} textAnchor="middle">
              {hr}
            </text>
          </g>
        ))}
      {yTicks.map((v, k) => (
        <g key={"y" + k}>
          <line x1={padL} y1={y(v)} x2={padL + w} y2={y(v)}
                stroke="#1b2028" strokeWidth={1} />
          <text x={padL - 5} y={y(v) + FS / 3} fill="#8a93a3" fontSize={FS}
                textAnchor="end">
            {v.toFixed(yDec)}
          </text>
        </g>
      ))}
      {/* axis titles: quantity / unit */}
      {xTitle && (
        <text x={padL + w / 2} y={height - 8} fill="#8a93a3" fontSize={FS}
              textAnchor="middle">
          {xTitle}
        </text>
      )}
      {yTitle && (
        <text x={13} y={pad + h / 2} fill="#8a93a3" fontSize={FS} textAnchor="middle"
              transform={`rotate(-90, 13, ${pad + h / 2})`}>
          {yTitle}
        </text>
      )}
      <line x1={padL} y1={y0} x2={padL + w} y2={y0} stroke="#3a4250" strokeWidth={1} />
      <path d={area} fill={fill} stroke="none" />
      <path d={path(main)} fill="none" stroke={color} strokeWidth={1.8} />
      {over && (
        <path d={path(over)} fill="none" stroke={overlayColor} strokeWidth={1.2} strokeDasharray="4 3" />
      )}
      {marker && marker > 0 && (
        <>
          <line x1={padL} y1={y(marker)} x2={padL + w} y2={y(marker)}
                stroke="#e5534b" strokeWidth={1.2} strokeDasharray="6 4" />
          {min < -marker * 0.5 && (
            <line x1={padL} y1={y(-marker)} x2={padL + w} y2={y(-marker)}
                  stroke="#e5534b" strokeWidth={1.2} strokeDasharray="6 4" />
          )}
          <text x={padL + w} y={y(marker) - 4} fill="#e5534b" fontSize="10" textAnchor="end">
            {marker.toFixed(0)}
          </text>
        </>
      )}
    </svg>
  );
}
