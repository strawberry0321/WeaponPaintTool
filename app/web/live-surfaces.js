import * as THREE from 'three';
import { hasBrush, lightFrame, MAX_LIGHT_REGIONS } from './emission.js';

function projectionNormals(geometry) {
  if (geometry.attributes.paintTriangleNormal) return;
  const positions = geometry.attributes.position;
  if (!positions) return;
  const values = new Float32Array(positions.count * 3);
  const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
  for (let index = 0; index < positions.count; index += 3) {
    a.fromBufferAttribute(positions, index); b.fromBufferAttribute(positions, index + 1); c.fromBufferAttribute(positions, index + 2);
    b.sub(a).cross(c.sub(a)).normalize();
    for (let corner = 0; corner < 3; corner++) values.set(b.toArray(), (index + corner) * 3);
  }
  geometry.setAttribute('paintTriangleNormal', new THREE.BufferAttribute(values, 3));
}

// Texture content stays shared. Dragging changes projection uniforms only.
export class LiveSurfaces {
  constructor(renderer) {
    this.renderer = renderer;
    this.textures = new Map();
    this.materials = {};
    this.overlays = new Map();
    this.pending = Promise.resolve();
  }

  async setPack(hash) {
    this.pack = hash;
    const response = await fetch(`/native-materials/${hash}/materials.json`);
    this.materials = response.ok ? await response.json() : {};
  }

  texture(url, color = false, repeat = false) {
    const key = `${url}|${color}|${repeat}`;
    if (!this.textures.has(key)) {
      const promise = new THREE.TextureLoader().loadAsync(url).then(texture => {
        texture.flipY = false;
        texture.colorSpace = color ? THREE.SRGBColorSpace : THREE.NoColorSpace;
        texture.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
        texture.wrapS = texture.wrapT = repeat ? THREE.RepeatWrapping : THREE.ClampToEdgeWrapping;
        return texture;
      });
      this.textures.set(key, promise);
    }
    return this.textures.get(key);
  }

  regionMasks(regions) {
    const key = 'light-masks/' + regions.map(region => region.maskHash).join('/');
    if (!this.textures.has(key)) {
      const load = Promise.all(regions.map(async region => {
        const image = new Image();
        image.src = `/images/${region.maskHash}.png`;
        await image.decode();
        return image;
      })).then(images => {
        const scale = Math.min(1, 1200 / Math.max(images[0].width, images[0].height));
        const canvas = document.createElement('canvas');
        const width = canvas.width = Math.max(1, Math.round(images[0].width * scale));
        const height = canvas.height = Math.max(1, Math.round(images[0].height * scale));
        const context = canvas.getContext('2d', { willReadFrequently: true });
        const pixels = new Uint8Array(width * height * regions.length);
        for (const [index, image] of images.entries()) {
          context.clearRect(0, 0, width, height);
          context.drawImage(image, 0, 0, width, height);
          const rgba = context.getImageData(0, 0, width, height).data;
          const offset = index * width * height;
          for (let i = 0; i < width * height; i++) pixels[offset + i] = Math.round(rgba[i * 4] * rgba[i * 4 + 3] / 255);
        }
        const texture = new THREE.DataArrayTexture(pixels, width, height, regions.length);
        texture.format = THREE.RedFormat;
        texture.minFilter = texture.magFilter = THREE.LinearFilter;
        texture.needsUpdate = true;
        return texture;
      });
      this.textures.set(key, load);
      load.catch(() => this.textures.delete(key));
    }
    return this.textures.get(key);
  }

  async nativeMaterials(part) {
    const groups = part.materialGroups || [];
    const neutral = () => new THREE.MeshStandardMaterial({ color: '#656b78', roughness: .65, metalness: .2, side: THREE.DoubleSide });
    if (!groups.length) return { materials: [neutral()], groups: [] };
    const identities = [...new Set(groups.map(group => group.previewMaterial || group.material))];
    const materials = await Promise.all(identities.map(async identity => {
      const description = this.materials[identity];
      if (!description) return neutral();
      const [base, data, normal] = await Promise.all(['base', 'mra', 'normal'].map(channel =>
        this.texture(`/native-materials/${this.pack}/${description.files[channel]}`, channel === 'base', true)));
      const material = new THREE.MeshStandardMaterial({ color: 'white', map: base,
        metalnessMap: data, roughnessMap: data, metalness: 1, roughness: 1,
        normalMap: normal, side: THREE.DoubleSide, envMapIntensity: .85 });
      material.normalScale.set(1, -1);
      material.onBeforeCompile = shader => {
        shader.fragmentShader = shader.fragmentShader.replace('metalnessFactor *= texelMetalness.b;', 'metalnessFactor *= texelMetalness.r;');
      };
      material.userData.nativeMaterial = identity;
      return material;
    }));
    return { materials, groups: groups.map(group => ({ ...group, index: identities.indexOf(group.previewMaterial || group.material) })) };
  }

  clear() {
    for (const overlay of this.overlays.values()) this.remove(overlay);
    this.overlays.clear();
  }

  async prune(meshes) {
    const used = new Set();
    for (const mesh of meshes.values()) {
      for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
        for (const key of ['map', 'normalMap', 'roughnessMap', 'metalnessMap']) if (material[key]) used.add(material[key]);
      }
    }
    for (const overlay of this.overlays.values()) {
      if (overlay.material.map) used.add(overlay.material.map);
      const mask = overlay.userData.uniforms.paintMaskTexture.value;
      if (mask) used.add(mask);
      const regions = overlay.userData.uniforms.paintRegionMasks.value;
      if (regions) used.add(regions);
    }
    for (const [url, promise] of this.textures) {
      const texture = await promise;
      if (!used.has(texture)) { texture.dispose(); this.textures.delete(url); }
    }
  }

  remove(overlay) {
    overlay.removeFromParent();
    if (overlay.userData.ownsGeometry) overlay.geometry.dispose();
    overlay.material.dispose();
    overlay.userData.uniforms.paintBrushData.value?.dispose();
  }

  applies(layer, part) {
    if (layer.enabled === false) return false;
    const target = layer.target || {};
    const identity = part.paintSourceId || part.instanceId;
    if (target.excludedComponentInstanceIds?.includes(identity)) return false;
    const listed = target.componentInstanceIds || [];
    if (part.role === 'ammo') {
      return listed.includes(identity) || Boolean(target.ammoHosts?.includes(part.hostEntityId)) ||
        (target.ammoHosts == null && listed.includes(part.ammoSyncSourceId));
    }
    if ((target.scope || 'assembly') === 'assembly') {
      return (target.assemblyWeaponIds || [layer.weaponId]).includes(part.hostEntityId);
    }
    return listed.includes(identity);
  }

  geometry(mesh, layer) {
    if (layer.coverageMode !== 'quads') return mesh.geometry;
    const part = mesh.userData.part;
    const identity = part.paintSourceId || part.instanceId;
    const source = layer.coverageSources?.[identity];
    if (source && source.unitId !== part.unitId) return new THREE.BufferGeometry();
    const position = [], normal = [], uv = [];
    const sourcePosition = mesh.geometry.attributes.position;
    const sourceNormal = mesh.geometry.attributes.normal;
    const sourceUv = mesh.geometry.attributes.uv;
    const vertex = (triangle, weights) => {
      for (const [attribute, destination] of [[sourcePosition, position], [sourceNormal, normal], [sourceUv, uv]]) {
        for (let channel = 0; channel < attribute.itemSize; channel++) {
          destination.push(weights.reduce((sum, weight, corner) => sum + weight * attribute.array[(triangle * 3 + corner) * attribute.itemSize + channel], 0));
        }
      }
    };
    for (const index of layer.coverage?.[identity] || []) {
      const quad = part.quads[index];
      if (!quad) continue;
      const [triangle, other, corner] = quad;
      if (other >= 0) {
        for (const face of [triangle, other]) for (let i = 0; i < 3; i++) vertex(face, [0, 1, 2].map(j => Number(i === j)));
      } else {
        const a = [0, 1, 2].map(i => Number(i === corner));
        const b = [0, 1, 2].map(i => i === corner || i === (corner + 1) % 3 ? .5 : 0);
        const c = [1 / 3, 1 / 3, 1 / 3];
        const d = [0, 1, 2].map(i => i === corner || i === (corner + 2) % 3 ? .5 : 0);
        for (const weights of [a, b, c, a, c, d]) vertex(triangle, weights);
      }
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(position, 3));
    geometry.setAttribute('normal', new THREE.Float32BufferAttribute(normal, 3));
    geometry.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    return geometry;
  }

  async create(mesh, layer, order, geometryKey) {
    const geometry = this.geometry(mesh, layer);
    projectionNormals(geometry);
    const material = new THREE.MeshStandardMaterial({ color: 'white', roughness: .65, metalness: .2,
      transparent: true, depthWrite: false, polygonOffset: true,
      polygonOffsetFactor: -1, polygonOffsetUnits: -(order + 1), side: THREE.DoubleSide,
      toneMapped: !layer.imageHash && layer.kind !== 'emission' });
    if (layer.imageHash) material.map = await this.texture(`/images/${layer.imageHash}.png`, true);
    const regions = layer.emission?.regions;
    const brush = hasBrush(layer);
    const maskTexture = layer.imageHash && layer.emission?.maskHash ? await this.texture(`/images/${layer.emission.maskHash}.png`) : null;
    const regionMasks = regions?.length ? await this.regionMasks(regions) : null;
    const uniforms = {
      paintOrigin: { value: new THREE.Vector3() }, paintU: { value: new THREE.Vector3(1, 0, 0) },
      paintV: { value: new THREE.Vector3(0, 0, 1) }, paintNormal: { value: new THREE.Vector3(0, -1, 0) },
      paintSize: { value: new THREE.Vector2(1, 1) }, paintMove: { value: new THREE.Vector2() },
      paintAngle: { value: 0 }, paintDepth: { value: 1 }, paintInward: { value: 0 }, paintCutoff: { value: .001 },
      paintCrop: { value: new THREE.Vector4(0, 0, 1, 1) }, paintAssembly: { value: mesh.userData.assembled.clone() },
      paintNormalMatrix: { value: new THREE.Matrix3().getNormalMatrix(mesh.userData.assembled) },
      paintLight: { value: new THREE.Color(0) }, paintOnlyLight: { value: 0 },
      paintSweep: { value: new THREE.Vector4(0, 0, 0, 0) }, paintSweepRange: { value: new THREE.Vector2(0, 1) },
      paintRegionMasks: { value: regionMasks }, paintRegionCount: { value: regions?.length || 0 },
      paintRegionColors: { value: Array.from({ length: MAX_LIGHT_REGIONS }, () => new THREE.Color(0)) },
      paintRegionTint: { value: new Float32Array(MAX_LIGHT_REGIONS) },
      paintCylinder: { value: 0 }, paintRadius: { value: .1 },
      paintBrushData: { value: null }, paintBrushCount: { value: 0 }, paintBrushWidth: { value: 1 }, paintBrushHardness: { value: .7 },
      paintMaskTexture: { value: maskTexture }, paintHasMask: { value: Number(Boolean(maskTexture)) }
    };
    material.onBeforeCompile = shader => {
      Object.assign(shader.uniforms, uniforms);
      shader.vertexShader = `varying vec3 paintPosition; varying vec3 paintSurfaceNormal; attribute vec3 paintTriangleNormal;
        uniform mat4 paintAssembly; uniform mat3 paintNormalMatrix;\n` + shader.vertexShader;
      shader.vertexShader = shader.vertexShader.replace('#include <begin_vertex>', `#include <begin_vertex>
        paintPosition = (paintAssembly * vec4(position, 1.0)).xyz;
        paintSurfaceNormal = normalize(paintNormalMatrix * paintTriangleNormal);`);
      shader.fragmentShader = `varying vec3 paintPosition; varying vec3 paintSurfaceNormal;
        uniform vec3 paintOrigin, paintU, paintV, paintNormal, paintLight;
        uniform vec2 paintSize, paintMove;
        uniform float paintAngle, paintDepth, paintInward, paintCutoff, paintOnlyLight, paintCylinder, paintRadius;
        uniform sampler2D paintMaskTexture; uniform float paintHasMask;
        uniform sampler2D paintBrushData; uniform int paintBrushCount; uniform float paintBrushWidth, paintBrushHardness;
        uniform vec4 paintCrop, paintSweep; uniform vec2 paintSweepRange;
        ${regionMasks ? `uniform highp sampler2DArray paintRegionMasks; uniform int paintRegionCount;
          uniform vec3 paintRegionColors[${MAX_LIGHT_REGIONS}]; uniform float paintRegionTint[${MAX_LIGHT_REGIONS}];` : ''}\n` + shader.fragmentShader;
      if (layer.imageHash) {
        shader.fragmentShader = shader.fragmentShader.replace('#include <map_fragment>', `
          vec3 relative = paintPosition - paintOrigin;
          float px = dot(relative, paintU), py = dot(relative, paintV), pz = dot(relative, paintNormal);
          if (paintCylinder > 0.5) {
            float radius = length(vec2(px, pz));
            if (radius < .000001 || dot(paintSurfaceNormal, paintU) * px + dot(paintSurfaceNormal, paintNormal) * pz <= paintCutoff * radius) discard;
            px = paintRadius * atan(px, pz);
            pz = radius - paintRadius;
          }
          else if (dot(normalize(paintSurfaceNormal), paintNormal) <= paintCutoff) discard;
          vec2 xy = vec2(px, py) - paintMove;
          if (paintCylinder > 0.5) xy.x = mod(xy.x + 3.14159265359 * paintRadius, 6.28318530718 * paintRadius) - 3.14159265359 * paintRadius;
          float ca = cos(paintAngle), sa = sin(paintAngle);
          vec2 imageUv = vec2((ca * xy.x + sa * xy.y) / paintSize.x + .5, .5 - (-sa * xy.x + ca * xy.y) / paintSize.y);
          bool outsideDepth = paintInward > .5 ? pz > .00002 || pz < -paintDepth - .00002 : abs(pz) > paintDepth;
          if (outsideDepth || any(lessThan(imageUv, vec2(0.0))) || any(greaterThan(imageUv, vec2(1.0)))) discard;
          vec4 artwork = texture2D(map, mix(paintCrop.xy, paintCrop.zw, imageUv));
          if (artwork.a < .002) discard;
          diffuseColor *= artwork;`);
      }
      if (brush) {
        shader.fragmentShader = shader.fragmentShader.replace('#include <map_fragment>', `
          float brushCoverage = 0.0;
          for (int stampIndex = 0; stampIndex < paintBrushCount; stampIndex++) {
            float column = (float(stampIndex) + .5) / paintBrushWidth;
            vec4 stamp = texture2D(paintBrushData, vec2(column, .25));
            vec3 stampNormal = texture2D(paintBrushData, vec2(column, .75)).xyz;
            vec3 brushRelative = paintPosition - stamp.xyz;
            float brushDepth = dot(brushRelative, stampNormal);
            float brushDistance = length(brushRelative - stampNormal * brushDepth) / stamp.w;
            float brushAlpha = 1.0 - smoothstep(paintBrushHardness, 1.0, brushDistance);
            if (dot(normalize(paintSurfaceNormal), stampNormal) > .05 && abs(brushDepth) <= stamp.w * .5) brushCoverage = max(brushCoverage, brushAlpha);
          }
          if (brushCoverage < .002) discard;
          diffuseColor.a *= brushCoverage;`);
      }
      shader.fragmentShader = shader.fragmentShader.replace('#include <emissivemap_fragment>', `#include <emissivemap_fragment>
        vec3 paintGlow = paintLight;
        vec3 paintRadiance = vec3(0.0);
        if (paintSweep.y > 0.0) {
          float progress = clamp((paintPosition.x - paintSweepRange.x) / (paintSweepRange.y - paintSweepRange.x), 0.0, 1.0);
          if (paintSweep.z > 0.5) progress = 1.0 - progress;
          float distanceToHead = paintSweep.x - progress;
          paintGlow *= smoothstep(0.0, paintSweep.y * 0.2, distanceToHead)
            * (1.0 - smoothstep(paintSweep.y * 0.2, paintSweep.y, distanceToHead));
        }
        ${regionMasks ? `
          vec2 regionUv = mix(paintCrop.xy, paintCrop.zw, imageUv);
          for (int i = 0; i < ${MAX_LIGHT_REGIONS}; i++) {
            if (i >= paintRegionCount) break;
            float mask = texture(paintRegionMasks, vec3(regionUv, float(i))).r;
            vec3 tint = mix(vec3(1.0), diffuseColor.rgb, paintRegionTint[i]);
            paintRadiance += paintGlow * paintRegionColors[i] * tint * mask;
          }
        ` : `
          ${layer.imageHash ? 'if (paintHasMask > .5) { vec4 lightMask = texture2D(paintMaskTexture, mix(paintCrop.xy, paintCrop.zw, imageUv)); paintGlow *= lightMask.r * lightMask.a; }' : ''}
          paintRadiance += diffuseColor.rgb * paintGlow;
        `}`);
      shader.fragmentShader = shader.fragmentShader.replace(
        'vec3 outgoingLight = totalDiffuse + totalSpecular + totalEmissiveRadiance;',
        `// Preserve chromaticity at high brightness on the editor's SDR display.
        float peak = max(max(paintRadiance.r, paintRadiance.g), paintRadiance.b);
        vec3 lightColor = paintRadiance / max(peak, .00001);
        float coverage = 1.0 - exp(-1.4 * peak);
        vec3 outgoingLight = totalDiffuse + totalSpecular + totalEmissiveRadiance;
        if (paintOnlyLight > .5) {
          diffuseColor.a *= coverage;
          outgoingLight = lightColor;
        } else {
          outgoingLight = mix(outgoingLight, lightColor, coverage);
        }`);
    };
    material.customProgramCacheKey = () => `live-paint-${Boolean(layer.imageHash)}-${brush}-${Boolean(regionMasks)}`;
    const overlay = new THREE.Mesh(geometry, material);
    overlay.renderOrder = 1 + order;
    overlay.userData = { layerId: layer.layerId, geometryKey, uniforms,
      ownsGeometry: geometry !== mesh.geometry, parentMesh: mesh, imageHash: layer.imageHash };
    return overlay;
  }

  update(overlay, layer, meshes) {
    const material = overlay.material;
    const oneSide = layer.imageHash && layer.projection?.type !== 'cylinder' && layer.projection?.wrap === false;
    const side = hasBrush(layer) || oneSide ? THREE.FrontSide : THREE.DoubleSide;
    if (material.side !== side) { material.side = side; material.needsUpdate = true; }
    material.color.set(layer.imageHash || layer.kind === 'emission' ? '#ffffff' : layer.color || '#ffffff');
    material.opacity = layer.opacity ?? 1;
    material.roughness = layer.roughness ?? .65;
    material.metalness = layer.metalness ?? .2;
    const uniforms = overlay.userData.uniforms;
    const owner = overlay.userData.parentMesh;
    const mesh = meshes.get(owner.userData.part.paintSourceId) || owner;
    uniforms.paintAssembly.value.copy(mesh.userData.assembled);
    uniforms.paintNormalMatrix.value.getNormalMatrix(mesh.userData.assembled);
    if (hasBrush(layer)) {
      const part = mesh.userData.part;
      const stamps = layer.brush.stamps.filter(stamp => stamp.partId === part.instanceId && stamp.unitId === part.unitId);
      const key = JSON.stringify(stamps);
      if (overlay.userData.brushKey !== key) {
        const width = Math.max(1, stamps.length), values = new Float32Array(width * 8);
        for (const [index, stamp] of stamps.entries()) {
          const position = new THREE.Vector3(...stamp.position).applyMatrix4(mesh.userData.assembled);
          const normal = new THREE.Vector3(...stamp.normal).applyMatrix3(new THREE.Matrix3().getNormalMatrix(mesh.userData.assembled)).normalize();
          values.set([...position.toArray(), stamp.radius], index * 4);
          values.set([...normal.toArray(), 0], (width + index) * 4);
        }
        uniforms.paintBrushData.value?.dispose();
        const texture = new THREE.DataTexture(values, width, 2, THREE.RGBAFormat, THREE.FloatType);
        texture.needsUpdate = true;
        uniforms.paintBrushData.value = texture; uniforms.paintBrushCount.value = stamps.length; uniforms.paintBrushWidth.value = width;
        overlay.userData.brushKey = key;
      }
      uniforms.paintBrushHardness.value = layer.brush.hardness ?? .7;
    }
    const projection = layer.projection;
    if (projection) {
      const origin = new THREE.Vector3(...projection.origin);
      const u = new THREE.Vector3(...projection.u), v = new THREE.Vector3(...projection.v), normal = new THREE.Vector3(...projection.normal);
      const anchored = meshes.get(layer.anchor?.instanceId);
      if (anchored && layer.anchor.assembledTransform) {
        const delta = anchored.userData.assembled.clone().multiply(new THREE.Matrix4().set(...layer.anchor.assembledTransform.flat()).invert());
        origin.applyMatrix4(delta);
        const rotation = new THREE.Matrix3().setFromMatrix4(delta);
        u.applyMatrix3(rotation); v.applyMatrix3(rotation); normal.applyMatrix3(rotation);
      }
      uniforms.paintOrigin.value.copy(origin);
      uniforms.paintU.value.copy(u); uniforms.paintV.value.copy(v); uniforms.paintNormal.value.copy(normal);
      const placement = layer.placement || {};
      const scale = placement.scale || 1;
      const circumference = projection.type === 'cylinder' ? 2 * Math.PI * projection.radius : Infinity;
      uniforms.paintSize.value.set(Math.min(projection.width * scale, circumference), projection.height * scale);
      uniforms.paintMove.value.set(placement.x || 0, placement.y || 0);
      uniforms.paintAngle.value = THREE.MathUtils.degToRad(placement.angle || 0);
      uniforms.paintDepth.value = projection.depth;
      uniforms.paintInward.value = Number(projection.depthMode === 'inward' && projection.type !== 'cylinder');
      uniforms.paintCutoff.value = projection.wrap === false ? (projection.cutoff ?? .001) : -2;
      uniforms.paintCrop.value.fromArray(layer.crop || [0, 0, 1, 1]);
      uniforms.paintCylinder.value = Number(projection.type === 'cylinder');
      uniforms.paintRadius.value = projection.radius || .1;
    }
    for (const [index, region] of (layer.emission?.regions || []).entries()) {
      uniforms.paintRegionColors.value[index].set(region.color);
      uniforms.paintRegionTint.value[index] = Number(Boolean(region.useImageColor));
    }
    this.updateLight(overlay, layer, this.seconds || 0);
    uniforms.paintOnlyLight.value = Number(layer.kind === 'emission');
    overlay.visible = layer.enabled !== false;
  }

  updateLight(overlay, layer, seconds, steady = false) {
    const uniforms = overlay.userData.uniforms;
    const light = layer.emission || {};
    const frame = lightFrame(light, seconds);
    const strength = steady ? (light.mode === 'breathe' ? light.maximum : light.intensity) : frame.strength;
    const enabled = light.enabled || layer.kind === 'emission';
    uniforms.paintLight.value.set(light.regions?.length ? '#ffffff' : light.color || (layer.imageHash ? '#ffffff' : layer.color || '#ffffff'));
    uniforms.paintLight.value.multiplyScalar(enabled ? strength : 0);
    uniforms.paintSweep.value.set(frame.head, !steady && enabled && light.mode === 'flow' ? light.width : 0,
      Number(light.direction === 'frontToBack'), 0);
    if (light.range) uniforms.paintSweepRange.value.fromArray(light.range);
  }

  refresh(project, meshes) {
    const layers = [...(project.colors || []), ...project.layers];
    const keys = new Set();
    const tasks = [];
    layers.forEach((layer, order) => {
      for (const [identity, mesh] of meshes) {
        if (!this.applies(layer, mesh.userData.part)) continue;
        const key = layer.layerId + '/' + identity;
        keys.add(key);
        const source = mesh.userData.part.paintSourceId || identity;
        const geometryKey = JSON.stringify([layer.coverageMode, layer.coverage?.[source], layer.coverageSources?.[source],
          layer.imageHash, layer.emission?.maskHash, layer.emission?.regions?.map(region => region.maskHash)]);
        let overlay = this.overlays.get(key);
        if (overlay && overlay.userData.geometryKey !== geometryKey) {
          this.remove(overlay);
          this.overlays.delete(key);
          overlay = null;
        }
        if (overlay) {
          overlay.renderOrder = 1 + order;
          overlay.material.polygonOffsetUnits = -(order + 1);
          this.update(overlay, layer, meshes);
        }
        else tasks.push(this.create(mesh, layer, order, geometryKey).then(created => {
          if (this.generation !== generation || !meshes.has(identity)) { this.remove(created); return; }
          mesh.add(created);
          this.overlays.set(key, created);
          this.update(created, layer, meshes);
        }));
      }
    });
    for (const [key, overlay] of this.overlays) {
      if (!keys.has(key)) { this.remove(overlay); this.overlays.delete(key); }
    }
    const generation = this.generation = (this.generation || 0) + 1;
    this.pending = Promise.all(tasks);
    return this.pending;
  }

  animate(project, meshes, seconds, steady = false) {
    this.seconds = seconds;
    for (const layer of project.layers) {
      if (layer.emission?.mode !== 'breathe' && layer.emission?.mode !== 'flow') continue;
      for (const [identity] of meshes) {
        const overlay = this.overlays.get(layer.layerId + '/' + identity);
        if (overlay) this.updateLight(overlay, layer, seconds, steady);
      }
    }
  }
}
