// Shapes are defined in viewport pixels, then sampled onto visible model surfaces.
export function brushPath(shape, start, end, spacing, reverse = false) {
  const dx = end.clientX - start.clientX;
  const dy = end.clientY - start.clientY;
  const length = Math.hypot(dx, dy);
  if (length < 1) return [start];
  const circle = shape === 'circle';
  const curve = shape === 'curve';
  const distance = circle ? 2 * Math.PI * length : curve ? length * 1.35 : length;
  const steps = Math.max(circle ? 12 : 1, Math.min(1024, Math.ceil(distance / Math.max(1, spacing))));
  const bend = reverse ? -.44 : .44;
  return Array.from({ length: steps + 1 }, (_, index) => {
    const t = index / steps;
    if (circle) {
      const angle = Math.atan2(dy, dx) + 2 * Math.PI * t;
      return { clientX: start.clientX + Math.cos(angle) * length, clientY: start.clientY + Math.sin(angle) * length };
    }
    const arc = curve ? 2 * t * (1 - t) * bend : 0;
    return { clientX: start.clientX + dx * t - dy * arc, clientY: start.clientY + dy * t + dx * arc };
  });
}
