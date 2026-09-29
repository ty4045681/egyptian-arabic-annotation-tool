/**
 * Waveform layout constants (work package F, workspace/media).
 *
 * Kept outside the panel component so `WaveformPanel.tsx` exports only the
 * component (react-refresh). P1 shows the waveform collapsed at ~120px so
 * the first transcript stays in the first viewport at 1366x768; expanding
 * doubles the height for boundary inspection.
 */

export const WAVEFORM_COLLAPSED_PX = 120;
export const WAVEFORM_EXPANDED_PX = 240;

export interface WaveformSegment {
  start: number;
  end: number;
  id: number | string;
}
