import * as THREE from 'three';

export function rebindAmmoLayer(layer, part) {
  const source = layer.ammoSource;
  if (!source || !layer.target?.ammoHosts?.includes(part.hostEntityId) ||
      source.unitId === part.unitId && source.componentId === part.componentId) return layer;
  const sourceSize = source.bounds[1].map((value, i) => Math.max(1e-6, value - source.bounds[0][i]));
  const targetSize = part.localBounds[1].map((value, i) => Math.max(1e-6, value - part.localBounds[0][i]));
  const sourceAxes = [0, 1, 2].sort((a, b) => sourceSize[a] - sourceSize[b]);
  const targetAxes = [0, 1, 2].sort((a, b) => targetSize[a] - targetSize[b]);
  const local = new THREE.Matrix4();
  local.elements.fill(0); local.elements[15] = 1;
  for (let i = 0; i < 3; i++) local.elements[sourceAxes[i] * 4 + targetAxes[i]] = targetSize[targetAxes[i]] / sourceSize[sourceAxes[i]];
  if (local.determinant() < 0) for (let i = 0; i < 3; i++) local.elements[i * 4 + targetAxes[0]] *= -1;
  const center = bounds => new THREE.Vector3(...bounds[0]).add(new THREE.Vector3(...bounds[1])).multiplyScalar(.5);
  local.setPosition(center(part.localBounds).sub(center(source.bounds).applyMatrix4(local)));
  const current = new THREE.Matrix4().set(...part.assembledTransform.flat());
  const original = new THREE.Matrix4().set(...source.assembledTransform.flat());
  const world = current.clone().multiply(local).multiply(original.clone().invert());
  const result = structuredClone(layer);
  if (result.projection) {
    const p = result.projection, linear = new THREE.Matrix3().setFromMatrix4(world);
    p.origin = new THREE.Vector3(...p.origin).applyMatrix4(world).toArray();
    const u = new THREE.Vector3(...p.u).applyMatrix3(linear), v = new THREE.Vector3(...p.v).applyMatrix3(linear);
    let sx = u.length();
    const sy = v.length();
    p.u = u.normalize().toArray(); p.v = v.normalize().toArray();
    if (p.type === 'cylinder') {
      const radial = new THREE.Vector3(...p.normal).applyMatrix3(linear);
      radial.addScaledVector(v, -radial.dot(v));
      sx = Math.sqrt(sx * radial.length());
      radial.normalize();
      p.normal = radial.toArray();
      p.u = v.clone().cross(radial).toArray();
      p.radius *= sx;
      p.axis = 'auto';
    } else {
      p.normal = new THREE.Vector3(...p.normal).applyMatrix3(new THREE.Matrix3().getNormalMatrix(world)).normalize().toArray();
    }
    p.width *= sx; p.height *= sy; p.depth *= Math.max(sx, sy);
    result.placement.x = (result.placement.x || 0) * sx; result.placement.y = (result.placement.y || 0) * sy;
    result.anchor = { instanceId: part.instanceId, localPosition: new THREE.Vector3(...p.origin).applyMatrix4(current.clone().invert()).toArray(),
      assembledTransform: structuredClone(part.assembledTransform) };
  }
  for (const stamp of result.brush?.stamps || []) {
    if (stamp.unitId !== source.unitId) continue;
    stamp.position = new THREE.Vector3(...stamp.position).applyMatrix4(local).toArray();
    stamp.normal = new THREE.Vector3(...stamp.normal).applyMatrix3(new THREE.Matrix3().getNormalMatrix(local)).normalize().toArray();
    stamp.radius *= Math.cbrt(Math.abs(local.determinant())); stamp.unitId = part.unitId; stamp.partId = part.instanceId;
  }
  result.ammoSource = { unitId: part.unitId, componentId: part.componentId, instanceId: part.instanceId,
    bounds: structuredClone(part.localBounds), assembledTransform: structuredClone(part.assembledTransform) };
  result.target.componentInstanceIds = [part.instanceId];
  return result;
}
