// M1: interface stub only — real EventSource wiring lands in M3 (T3.7).
export type SSEHandler = (event: string, data: unknown) => void;

export function useSSE(_onEvent: SSEHandler): { connected: boolean } {
  return { connected: false };
}