import { describe, expect, it } from "vitest";
import { axisTicks, chartExtent } from "./Sparkline";

// Regression: the trafo-rating marker used to be PUSHED into the main series
// (same array reference when no overlay was drawn) and was then rendered as a
// phantom ~250-kW peak at 24:00. The extent must include the marker headroom
// WITHOUT touching the plotted data.
describe("chartExtent", () => {
  it("does not mutate the series when a marker is set (no overlay)", () => {
    const main = [10, 20, 15, 12];
    const before = [...main];
    const { max } = chartExtent(main, null, 238);
    expect(main).toEqual(before);          // no phantom point at the end
    expect(main.length).toBe(4);
    expect(max).toBeCloseTo(238 * 1.05);   // marker stays in view via headroom
  });

  it("does not mutate series or overlay when both are drawn", () => {
    const main = [10, 20];
    const over = [30, 5];
    const { max, min } = chartExtent(main, over, 100);
    expect(main).toEqual([10, 20]);
    expect(over).toEqual([30, 5]);
    expect(max).toBeCloseTo(105);
    expect(min).toBe(0);                   // zero baseline for positive data
  });

  it("keeps the data extent without a marker and spans negatives", () => {
    expect(chartExtent([1, 2, 3], null)).toEqual({ min: 0, max: 3 });
    const { min, max } = chartExtent([-26, 218.7], null, 237.5);
    expect(min).toBe(-26);                 // PV reverse flow stays visible
    expect(max).toBeCloseTo(237.5 * 1.05);
  });
});

describe("axisTicks", () => {
  it("returns five evenly spaced ticks including min and max", () => {
    expect(axisTicks(0, 100)).toEqual([0, 25, 50, 75, 100]);
    const t = axisTicks(-26, 250);
    expect(t).toHaveLength(5);
    expect(t[0]).toBe(-26);
    expect(t[4]).toBe(250);
    expect(t[2]).toBeCloseTo((-26 + 250) / 2);   // evenly distributed
  });
});
