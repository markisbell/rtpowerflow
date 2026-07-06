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
}: Props) {
  if (!values.length) return null;
  const pad = 26;
  const w = width - pad * 2;
  const h = height - pad * 2;

  const ds = (arr: number[]) => {
    const stride = Math.max(1, Math.floor(arr.length / 280));
    const out: number[] = [];
    for (let i = 0; i < arr.length; i += stride) out.push(arr[i]);
    return out;
  };
  const main = ds(values);
  const over = overlay ? ds(overlay) : null;

  const all = over ? [...main, ...over] : main;
  if (marker && marker > 0) all.push(marker * 1.05);   // keep the limit in view
  const max = Math.max(...all, 1e-9);
  const min = Math.min(...all, 0);
  const span = max - min || 1e-9;

  const x = (i: number, n: number) => pad + (i / (n - 1)) * w;
  const y = (v: number) => pad + h - ((v - min) / span) * h;
  const y0 = y(0);

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
  const area = `${path(main)} L${x(main.length - 1, main.length).toFixed(1)},${y0} L${pad},${y0} Z`;

  return (
    <svg width={fluid ? undefined : width} height={fluid ? undefined : height}
         viewBox={`0 0 ${width} ${height}`}
         style={fluid ? { width: "100%", height: "auto", display: "block" } : undefined}
         role="img" aria-label="daily load profile">
      <line x1={pad} y1={y0} x2={pad + w} y2={y0} stroke="#3a4250" strokeWidth={1} />
      <path d={area} fill={fill} stroke="none" />
      <path d={path(main)} fill="none" stroke={color} strokeWidth={1.8} />
      {over && (
        <path d={path(over)} fill="none" stroke={overlayColor} strokeWidth={1.2} strokeDasharray="4 3" />
      )}
      {marker && marker > 0 && (
        <>
          <line x1={pad} y1={y(marker)} x2={pad + w} y2={y(marker)}
                stroke="#e5534b" strokeWidth={1.2} strokeDasharray="6 4" />
          {min < -marker * 0.5 && (
            <line x1={pad} y1={y(-marker)} x2={pad + w} y2={y(-marker)}
                  stroke="#e5534b" strokeWidth={1.2} strokeDasharray="6 4" />
          )}
          <text x={pad + w} y={y(marker) - 4} fill="#e5534b" fontSize="10" textAnchor="end">
            {marker.toFixed(0)}
          </text>
        </>
      )}
      <text x={2} y={pad + 4} fill="#8a93a3" fontSize="10">{max.toFixed(max < 10 ? 1 : 0)}</text>
      <text x={2} y={y0 + 3} fill="#8a93a3" fontSize="10">0</text>
      {min < 0 && <text x={2} y={pad + h} fill="#8a93a3" fontSize="10">{min.toFixed(1)}</text>}
      {hourAxis &&
        [0, 6, 12, 18, 24].map((hr) => (
          <text key={hr} x={pad + (hr / 24) * w} y={height - 6} fill="#8a93a3" fontSize="10" textAnchor="middle">
            {hr}:00
          </text>
        ))}
    </svg>
  );
}
