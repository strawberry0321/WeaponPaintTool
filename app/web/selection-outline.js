import * as THREE from 'three';

// Screen-space widths keep small face selections readable at every zoom level.
export function selectionOutlines(geometry, whole, resolution) {
  if (whole) {
    return [6, 3].map((width, index) => new THREE.Mesh(geometry.clone(), new THREE.ShaderMaterial({
      side: THREE.BackSide, depthWrite: false,
      uniforms: { resolution: { value: resolution }, width: { value: width }, color: { value: new THREE.Color(index ? '#245cdb' : '#ffffff') } },
      vertexShader: `uniform vec2 resolution; uniform float width;
        void main() {
          vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          vec2 direction = (projectionMatrix * vec4(normalMatrix * normal, 0.0)).xy;
          p.xy += direction / max(length(direction), .0001) * width * 2.0 / resolution * p.w;
          gl_Position = p;
        }`,
      fragmentShader: 'uniform vec3 color; void main() { gl_FragColor = vec4(color, 1.0); }'
    })));
  }
  const edges = new Map(), positions = geometry.attributes.position;
  for (let vertex = 0; vertex < positions.count; vertex += 3) {
    for (let corner = 0; corner < 3; corner++) {
      const a = new THREE.Vector3().fromBufferAttribute(positions, vertex + corner);
      const b = new THREE.Vector3().fromBufferAttribute(positions, vertex + (corner + 1) % 3);
      const key = [a, b].map(point => point.toArray().map(value => Math.round(value * 1e5)).join(',')).sort().join('/');
      if (edges.has(key)) edges.delete(key);
      else edges.set(key, [a, b]);
    }
  }
  const position = [], other = [], side = [];
  for (const [a, b] of edges.values()) {
    for (const [point, opposite, sign] of [[a, b, -1], [a, b, 1], [b, a, 1], [a, b, -1], [b, a, 1], [b, a, -1]]) {
      position.push(...point.toArray()); other.push(...opposite.toArray());
      side.push(point === a ? sign : -sign);
    }
  }
  return [5, 2.5].map((width, index) => {
    const boundary = new THREE.BufferGeometry();
    boundary.setAttribute('position', new THREE.Float32BufferAttribute(position, 3));
    boundary.setAttribute('other', new THREE.Float32BufferAttribute(other, 3));
    boundary.setAttribute('edgeSide', new THREE.Float32BufferAttribute(side, 1));
    return new THREE.Mesh(boundary, new THREE.ShaderMaterial({
      depthWrite: false, side: THREE.DoubleSide,
      uniforms: { resolution: { value: resolution }, width: { value: width }, color: { value: new THREE.Color(index ? '#245cdb' : '#ffffff') } },
      vertexShader: `attribute vec3 other; attribute float edgeSide; uniform vec2 resolution; uniform float width;
        void main() {
          vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          vec4 q = projectionMatrix * modelViewMatrix * vec4(other, 1.0);
          vec2 direction = (q.xy / q.w - p.xy / p.w) * resolution;
          vec2 perpendicular = vec2(-direction.y, direction.x) / max(length(direction), .0001);
          p.xy += perpendicular * edgeSide * width / resolution * p.w;
          p.z -= .00002 * p.w;
          gl_Position = p;
        }`,
      fragmentShader: 'uniform vec3 color; void main() { gl_FragColor = vec4(color, 1.0); }'
    }));
  });
}
