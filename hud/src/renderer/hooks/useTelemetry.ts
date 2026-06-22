/**
 * useTelemetry — WebSocket client for the FRIDAY telemetry server.
 *
 * Connects to the localhost WS, parses TelemetryFrame JSON, auto-reconnects
 * with exponential backoff, and tolerates malformed frames without crashing.
 * The HUD is a pure consumer — it never sends messages.
 */

import { useEffect, useRef, useState, useCallback } from "react";
import type { TelemetryFrame, ConnectionStatus } from "../types";
import { parseFrame } from "../utils/parseFrame";

const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 10_000;
const BACKOFF_FACTOR = 2;

export interface UseTelemetryResult {
  frame: TelemetryFrame | null;
  status: ConnectionStatus;
}

export function useTelemetry(url: string): UseTelemetryResult {
  const [frame, setFrame] = useState<TelemetryFrame | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");

  // Ref so cleanup can cancel the pending reconnect without a stale closure.
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef<number>(BACKOFF_BASE_MS);
  const destroyedRef = useRef<boolean>(false);

  const connect = useCallback(() => {
    if (destroyedRef.current) return;
    setStatus("connecting");

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (destroyedRef.current) {
        ws.close();
        return;
      }
      backoffRef.current = BACKOFF_BASE_MS; // reset on successful connect
      setStatus("connected");
    };

    ws.onmessage = (event: MessageEvent) => {
      if (destroyedRef.current) return;
      const parsed = parseFrame(event.data as string);
      if (parsed !== null) {
        setFrame(parsed);
      }
      // silently ignore malformed frames — never crash the UI
    };

    ws.onerror = () => {
      // onclose fires immediately after onerror; handle reconnect there
    };

    ws.onclose = () => {
      if (destroyedRef.current) return;
      setStatus("disconnected");
      wsRef.current = null;

      const delay = backoffRef.current;
      backoffRef.current = Math.min(delay * BACKOFF_FACTOR, BACKOFF_MAX_MS);

      retryTimerRef.current = setTimeout(() => {
        if (!destroyedRef.current) connect();
      }, delay);
    };
  }, [url]);

  useEffect(() => {
    destroyedRef.current = false;
    connect();

    return () => {
      destroyedRef.current = true;
      if (retryTimerRef.current !== null) {
        clearTimeout(retryTimerRef.current);
        retryTimerRef.current = null;
      }
      if (wsRef.current !== null) {
        wsRef.current.onclose = null; // prevent reconnect loop on intentional teardown
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  return { frame, status };
}
