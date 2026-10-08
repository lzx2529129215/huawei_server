export const start: (dailyMiB: number, peakMiB: number, fileMiB: number, holdSeconds: number, lifetimeSeconds: number, caseId: string, dailyCaseId: string, directory: string, seed: number, awaitPeak: boolean) => void;
export const promote: (caseId: string) => boolean;
export const stop: () => void;
export const retouch: () => void;
export const status: () => string;
export const record: (event: string, level: number) => void;
