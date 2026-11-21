import * as THREE from 'three';

export class DecalEditor {
  constructor({ canvas, viewport, camera, controls, layer, meshes, applies, displayAnchor, begin, change, end }) {
    Object.assign(this, { canvas, viewport, camera, controls, layer, meshes, applies, displayAnchor, begin, change, end });
    this.guides = document.querySelector('#decal-guides');
    this.handles = document.querySelector('#decal-handles');
    this.actions = document.querySelector('#decal-actions');
    this.ray = new THREE.Raycaster();
    this.crop = false;
    this.drag = null;
    this.points = [];
    canvas.addEventListener('pointerdown', event => this.down(event, 'move'), true);
    for (const button of this.handles.querySelectorAll('[data-handle]')) {
      button.addEventListener('pointerdown', event => this.down(event, button.dataset.handle));
    }
    window.addEventListener('pointermove', event => this.move(event), true);
    window.addEventListener('pointerup', event => this.up(event), true);
    window.addEventListener('pointercancel', event => this.up(event), true);
    window.addEventListener('lostpointercapture', event => this.up(event), true);
    window.addEventListener('blur', () => this.finish());
  }

  frame(layer = this.layer(), display = true) {
    if (!layer?.projection) return null;
    const p = layer.projection, placement = layer.placement || {};
    const origin = new THREE.Vector3(...p.origin), u = new THREE.Vector3(...p.u), v = new THREE.Vector3(...p.v);
    const normal = new THREE.Vector3(...p.normal);
    const identity = layer.anchor?.instanceId;
    const anchor = (display && identity && this.displayAnchor(identity)) || this.meshes().get(identity);
    if (anchor && layer.anchor.assembledTransform) {
      const delta = anchor.userData.assembled.clone().multiply(new THREE.Matrix4().set(...layer.anchor.assembledTransform.flat()).invert());
      origin.applyMatrix4(delta);
      const rotation = new THREE.Matrix3().setFromMatrix4(delta);
      u.applyMatrix3(rotation); v.applyMatrix3(rotation); normal.applyMatrix3(rotation);
      if (display) {
        if (anchor.userData.workspaceOffset) origin.add(anchor.userData.workspaceOffset);
        origin.addScaledVector(anchor.userData.delta, anchor.userData.explosionFactor ?? this.distance ?? 0);
      }
    }
    const cylinder = p.type === 'cylinder';
    const axisOrigin = origin.clone();
    const radialU = u.clone(), radialNormal = normal.clone();
    if (cylinder) {
      const theta = (placement.x || 0) / p.radius;
      origin.addScaledVector(u, p.radius * Math.sin(theta)).addScaledVector(normal, p.radius * Math.cos(theta));
      u.copy(radialU).multiplyScalar(Math.cos(theta)).addScaledVector(radialNormal, -Math.sin(theta));
      normal.copy(radialNormal).multiplyScalar(Math.cos(theta)).addScaledVector(radialU, Math.sin(theta));
      origin.addScaledVector(v, placement.y || 0);
    } else origin.addScaledVector(u, placement.x || 0).addScaledVector(v, placement.y || 0);
    const angle = THREE.MathUtils.degToRad(placement.angle || 0);
    const right = u.clone().multiplyScalar(Math.cos(angle)).addScaledVector(v, Math.sin(angle));
    const up = v.clone().multiplyScalar(Math.cos(angle)).addScaledVector(u, -Math.sin(angle));
    const width = Math.min(p.width * (placement.scale || 1), cylinder ? 2 * Math.PI * p.radius : Infinity);
    const height = p.height * (placement.scale || 1);
    const surfacePoint = (x, y) => {
      if (!cylinder) return origin.clone().addScaledVector(right, x).addScaledVector(up, y);
      const arc = (placement.x || 0) + x * Math.cos(angle) - y * Math.sin(angle);
      const axial = (placement.y || 0) + x * Math.sin(angle) + y * Math.cos(angle);
      return axisOrigin.clone().addScaledVector(radialU, p.radius * Math.sin(arc / p.radius))
        .addScaledVector(radialNormal, p.radius * Math.cos(arc / p.radius)).addScaledVector(v, axial);
    };
    return { origin, u, v, normal, right, up, width, height, surfacePoint, cylinder, axisOrigin, radialU, radialNormal,
      radius: p.radius };
  }

  project(point, rect) {
    const p = point.clone().project(this.camera);
    return new THREE.Vector2((p.x + 1) * rect.width / 2, (1 - p.y) * rect.height / 2);
  }

  render(distance = 0) {
    this.distance = distance;
    const rect = this.viewport.getBoundingClientRect();
    if (this.guides.width !== Math.round(rect.width) || this.guides.height !== Math.round(rect.height)) {
      this.guides.width = Math.round(rect.width); this.guides.height = Math.round(rect.height);
    }
    const context = this.guides.getContext('2d');
    context.clearRect(0, 0, this.guides.width, this.guides.height);
    const layer = this.layer();
    const frame = this.frame(layer);
    const show = Boolean(this.interactive !== false && layer?.imageHash && layer.enabled !== false && frame && !document.querySelector('dialog[open]'));
    this.handles.hidden = this.actions.hidden = !show;
    if (!show) { this.points = []; return; }
    this.points = [[-1, 1], [1, 1], [1, -1], [-1, -1]].map(([x, y]) =>
      this.project(frame.surfacePoint(x * frame.width / 2, y * frame.height / 2), rect));
    context.beginPath();
    const corners = [[-1, 1], [1, 1], [1, -1], [-1, -1]];
    for (let edge = 0; edge < 4; edge++) {
      const start = corners[edge], end = corners[(edge + 1) % 4];
      for (let step = 0; step <= 24; step++) {
        const fraction = step / 24;
        const point = this.project(frame.surfacePoint((start[0] + (end[0] - start[0]) * fraction) * frame.width / 2,
          (start[1] + (end[1] - start[1]) * fraction) * frame.height / 2), rect);
        if (edge === 0 && step === 0) context.moveTo(point.x, point.y);
        else context.lineTo(point.x, point.y);
      }
    }
    context.closePath();
    context.strokeStyle = '#fff'; context.lineWidth = 3; context.stroke();
    context.strokeStyle = this.crop ? '#b86a36' : '#526baf'; context.lineWidth = 1.5;
    context.setLineDash(this.crop ? [6, 4] : []); context.stroke(); context.setLineDash([]);
    const midpoint = frame.cylinder ? this.project(frame.surfacePoint(0, frame.height / 2), rect) : this.points[0].clone().add(this.points[1]).multiplyScalar(.5);
    const center = this.project(frame.origin, rect);
    const direction = midpoint.clone().sub(center).normalize();
    const rotate = midpoint.clone().addScaledVector(direction, 27);
    context.beginPath(); context.moveTo(midpoint.x, midpoint.y); context.lineTo(rotate.x, rotate.y);
    context.strokeStyle = '#526baf'; context.lineWidth = 1; context.stroke();
    for (const [index, name] of ['nw', 'ne', 'se', 'sw'].entries()) {
      const button = this.handles.querySelector(`[data-handle="${name}"]`);
      button.style.left = `${this.points[index].x}px`; button.style.top = `${this.points[index].y}px`;
      button.hidden = Math.min(this.points[0].distanceTo(this.points[1]), this.points[1].distanceTo(this.points[2])) < 16 ||
        (frame.cylinder && frame.width >= 1.99 * Math.PI * frame.radius);
    }
    const button = this.handles.querySelector('[data-handle="rotate"]');
    button.style.left = `${rotate.x}px`; button.style.top = `${rotate.y}px`; button.hidden = this.crop;
    document.querySelector('#crop-image').classList.toggle('active', this.crop);
    document.querySelector('#decal-action-name').textContent = this.crop ? '拖动边角裁剪' : layer.name;
    const depth = document.querySelector('#decal-depth');
    if (document.activeElement !== depth) depth.value = Number((layer.projection.depth * 1000).toFixed(2));
    const fullwrap = document.querySelector('#decal-fullwrap');
    if (fullwrap) fullwrap.checked = layer.projection.wrap !== false;
  }

  cylinderCoordinates(event, frame) {
    const layer = this.layer();
    const rect = this.canvas.getBoundingClientRect();
    this.ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2), this.camera);
    const targets = [...this.meshes().values()].filter(mesh => mesh.visible);
    const hit = this.ray.intersectObjects(targets, false)[0];
    if (!hit || !this.applies(layer, hit.object.userData.part)) return null;
    const part = hit.object.userData.part;
    const identity = part.paintSourceId || part.instanceId;
    if (layer.coverageMode === 'quads' && !(layer.coverage?.[identity] || []).some(index => {
      const quad = part.quads[index];
      return quad && (quad[0] === hit.faceIndex || quad[1] === hit.faceIndex);
    })) return null;
    const relative = hit.point.clone().sub(frame.axisOrigin);
    return { x: frame.radius * Math.atan2(relative.dot(frame.radialU), relative.dot(frame.radialNormal)),
      y: relative.dot(frame.v) };
  }

  wrappedArc(x, radius) {
    const half = Math.PI * radius;
    return THREE.MathUtils.euclideanModulo(x + half, 2 * half) - half;
  }

  contains(event) {
    const rect = this.viewport.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    let inside = false;
    for (let i = 0, j = this.points.length - 1; i < this.points.length; j = i++) {
      const a = this.points[i], b = this.points[j];
      if ((a.y > y) !== (b.y > y) && x < (b.x - a.x) * (y - a.y) / (b.y - a.y) + a.x) inside = !inside;
    }
    if (inside) return true;
    const pointer = new THREE.Vector2(x, y);
    return this.points.some((start, index) => {
      const edge = this.points[(index + 1) % this.points.length].clone().sub(start);
      const fraction = THREE.MathUtils.clamp(pointer.clone().sub(start).dot(edge) / Math.max(1, edge.lengthSq()), 0, 1);
      return pointer.distanceTo(start.clone().addScaledVector(edge, fraction)) <= 6;
    });
  }

  planePoint(event, frame) {
    const rect = this.canvas.getBoundingClientRect();
    this.ray.setFromCamera(new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2), this.camera);
    if (Math.abs(this.ray.ray.direction.dot(frame.normal)) < .12) return null;
    return this.ray.ray.intersectPlane(new THREE.Plane().setFromNormalAndCoplanarPoint(frame.normal, frame.origin), new THREE.Vector3());
  }

  screenPoint(event, frame) {
    const rect = this.canvas.getBoundingClientRect();
    const center = this.project(frame.origin, rect);
    const metresPerPixel = 2 * this.camera.position.distanceTo(frame.origin) *
      Math.tan(THREE.MathUtils.degToRad(this.camera.fov) / 2) / rect.height;
    return frame.origin.clone().addScaledVector(frame.u, (event.clientX - rect.left - center.x) * metresPerPixel)
      .addScaledVector(frame.v, (center.y - event.clientY + rect.top) * metresPerPixel);
  }

  down(event, handle) {
    const layer = this.layer();
    if (this.interactive === false || this.drag || event.button !== 0 || event.altKey || !layer?.imageHash) return;
    let frame = this.frame(layer), polar = null;
    if (handle === 'move') {
      if (frame.cylinder) {
        polar = this.cylinderCoordinates(event, frame);
        if (!polar) return;
        const x = this.wrappedArc(polar.x - (layer.placement.x || 0), frame.radius);
        const y = polar.y - (layer.placement.y || 0), angle = THREE.MathUtils.degToRad(layer.placement.angle || 0);
        if (Math.abs(Math.cos(angle) * x + Math.sin(angle) * y) > frame.width / 2 ||
            Math.abs(-Math.sin(angle) * x + Math.cos(angle) * y) > frame.height / 2) return;
      } else if (!this.contains(event)) return;
    }
    event.preventDefault(); event.stopImmediatePropagation();
    if (this.begin() === false) return;
    frame = this.frame();
    const planarPoint = this.planePoint(event, frame);
    const enabled = this.controls.enabled;
    this.controls.enabled = false;
    this.drag = { handle, frame, point: planarPoint || this.screenPoint(event, frame), screen: !planarPoint,
      polar, lastPolar: polar, arc: 0, enabled, pointerId: event.pointerId,
      before: structuredClone(this.layer()), x: event.clientX, y: event.clientY, moved: false };
    event.target.setPointerCapture(event.pointerId);
    this.capture = event.target;
  }

  move(event) {
    const drag = this.drag;
    if (!drag || event.pointerId !== drag.pointerId) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 2 && !drag.moved) return;
    const layer = this.layer();
    const before = drag.before, frame = drag.frame;
    if (drag.polar) {
      const current = this.cylinderCoordinates(event, frame);
      if (!current) return;
      drag.arc += this.wrappedArc(current.x - drag.lastPolar.x, frame.radius);
      drag.lastPolar = current;
      layer.placement.x = (before.placement.x || 0) + drag.arc;
      layer.placement.y = (before.placement.y || 0) + current.y - drag.polar.y;
      drag.moved = true;
      this.change();
      return;
    }
    const point = drag.screen ? this.screenPoint(event, frame) : this.planePoint(event, frame);
    if (!point) return;
    drag.moved = true;
    const delta = point.clone().sub(drag.point);
    if (drag.handle === 'move') {
      layer.placement.x = (before.placement.x || 0) + delta.dot(frame.u);
      layer.placement.y = (before.placement.y || 0) + delta.dot(frame.v);
    } else if (drag.handle === 'rotate') {
      const start = drag.point.clone().sub(frame.origin), current = point.clone().sub(frame.origin);
      const angle = Math.atan2(current.dot(frame.v), current.dot(frame.u)) - Math.atan2(start.dot(frame.v), start.dot(frame.u));
      const degrees = (before.placement.angle || 0) + THREE.MathUtils.radToDeg(angle);
      layer.placement.angle = event.shiftKey ? Math.round(degrees / 15) * 15 : degrees;
    } else if (this.crop) {
      const old = before.crop || [0, 0, 1, 1];
      const crop = [...old];
      const dx = delta.dot(frame.right) / frame.width * (old[2] - old[0]);
      const dy = -delta.dot(frame.up) / frame.height * (old[3] - old[1]);
      const horizontal = drag.handle.includes('w') ? 0 : 2, vertical = drag.handle.includes('n') ? 1 : 3;
      crop[horizontal] = THREE.MathUtils.clamp(old[horizontal] + dx, horizontal === 0 ? 0 : old[0] + .01, horizontal === 0 ? old[2] - .01 : 1);
      crop[vertical] = THREE.MathUtils.clamp(old[vertical] + dy, vertical === 1 ? 0 : old[1] + .01, vertical === 1 ? old[3] - .01 : 1);
      layer.crop = crop;
      layer.projection.width = before.projection.width * (crop[2] - crop[0]) / (old[2] - old[0]);
      layer.projection.height = before.projection.height * (crop[3] - crop[1]) / (old[3] - old[1]);
      const shift = frame.right.clone().multiplyScalar((crop[0] + crop[2] - old[0] - old[2]) / 2 / (old[2] - old[0]) * frame.width)
        .addScaledVector(frame.up, -(crop[1] + crop[3] - old[1] - old[3]) / 2 / (old[3] - old[1]) * frame.height);
      layer.placement.x = (before.placement.x || 0) + shift.dot(frame.u);
      layer.placement.y = (before.placement.y || 0) + shift.dot(frame.v);
    } else {
      const start = drag.point.distanceTo(frame.origin), current = point.distanceTo(frame.origin);
      layer.placement.scale = THREE.MathUtils.clamp((before.placement.scale || 1) * current / Math.max(start, .0001), .01, 50);
    }
    this.change();
  }

  up(event) {
    if (!this.drag || event.pointerId !== this.drag.pointerId) return;
    event.preventDefault(); event.stopImmediatePropagation();
    this.finish();
  }

  finish() {
    if (!this.drag) return;
    const { moved, enabled, pointerId } = this.drag;
    this.drag = null;
    this.controls.enabled = enabled;
    if (this.capture?.hasPointerCapture(pointerId)) this.capture.releasePointerCapture(pointerId);
    this.end(moved);
  }
}
