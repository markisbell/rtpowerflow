import { useEffect, useState } from "react";
import { wsUrl } from "./api";
import type { StepResult } from "./types";

type Status = "connecting" | "open" | "closed";

/** Subscribes to /ws and exposes the latest StepResult, auto-reconnecting.
 *  Hardened against React 18 StrictMode double-mounts: a `stopped` flag plus
 *  per-socket guards ensure only the live socket updates state. */
export function useStepStream(enabled = true) {
  const [latest, setLatest] = useState<StepResult | null>(null);
  const [status, setStatus] = useState<Status>("closed");

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (stopped) return;
      setStatus("connecting");
      ws = new WebSocket(wsUrl());
      const self = ws;
      self.onopen = () => {
        if (!stopped && self === ws) setStatus("open");
      };
      self.onmessage = (ev) => {
        if (stopped || self !== ws) return;
        try {
          setLatest(JSON.parse(ev.data) as StepResult);
        } catch {
          /* ignore malformed frames */
        }
      };
      self.onclose = () => {
        if (stopped || self !== ws) return;
        setStatus("closed");
        retry = setTimeout(connect, 1500);
      };
      self.onerror = () => self.close();
    };
    connect();

    return () => {
      stopped = true;
      clearTimeout(retry);
      ws?.close();
      ws = null;
    };
  }, [enabled]);

  return { latest, status };
}
