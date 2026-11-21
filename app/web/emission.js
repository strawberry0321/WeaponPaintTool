export const MAX_LIGHT_REGIONS = 8;
export const LIGHT_MODES = { static: '常亮', breathe: '呼吸', flow: '流光' };
export const DEFAULT_LIGHT = Object.freeze({
  color: '#a4c8ff', intensity: 3, mode: 'static', minimum: .2, maximum: 3,
  period: 3, phase: 0, speed: .6, width: .25, direction: 'frontToBack'
});

export function hasBrush(layer) {
  return layer.kind === 'stroke' || layer.kind === 'emission' && Boolean(layer.brush);
}

export function lightFrame(settings, seconds) {
  if (settings.mode === 'breathe') {
    const wave = (1 - Math.cos(2 * Math.PI * seconds / Math.max(.1, settings.period ?? 3) + (settings.phase || 0))) / 2;
    const low = settings.minimum ?? .2, high = settings.maximum ?? 4;
    return { strength: low + (high - low) * wave, head: 0 };
  }
  const phase = seconds * (settings.speed ?? .6) + (settings.phase || 0) / (2 * Math.PI);
  return {
    strength: settings.intensity ?? 1,
    head: (phase - Math.floor(phase)) * (1 + (settings.width ?? .25))
  };
}
