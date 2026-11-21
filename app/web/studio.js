import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { LiveSurfaces } from './live-surfaces.js';
import { DecalEditor } from './decal-editor.js';
import { selectionOutlines } from './selection-outline.js';
import { rebindAmmoLayer } from './ammo-mapping.js';
import { DEFAULT_LIGHT, LIGHT_MODES } from './emission.js';
import { LightRegionEditor } from './light-region-editor.js';
import { brushPath } from './brush-path.js';

const $ = selector => document.querySelector(selector);
const clone = value => structuredClone(value);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
const state = { project: null, catalog: null, assembly: null, category: 'primary', layerId: null,
  undo: [], redo: [], meshes: new Map(), tool: 'select', library: [], jobId: null,
  loadGeneration: 0, importedPack: null, brushShapes: { paint: 'freehand', lightPaint: 'freehand' } };
let gestureBefore = null;
let saveTimer;
let toastTimer;
let cameraTimer;

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '本地请求失败');
  return result;
}

function toast(message, error = false) {
  const element = $('#toast');
  element.textContent = message;
  element.hidden = false;
  element.classList.toggle('error', error);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, error ? 10000 : 4500);
  $('#status').textContent = message;
}

function handleError(error) {
  console.error(error);
  toast(error.message || String(error), true);
}

function connect(selector, event, handler) {
  $(selector).addEventListener(event, async e => {
    try { await handler(e); } catch (error) { handleError(error); }
  });
}

function activeLayer() {
  return state.project?.layers.find(layer => layer.layerId === state.layerId);
}

const isArtwork = layer => Boolean(layer?.imageHash && layer.kind !== 'emission');
const activeDecal = () => isArtwork(activeLayer()) ? activeLayer() : null;
const isBrushTool = tool => tool === 'paint' || tool === 'lightPaint';

function editableLayer() {
  const layer = activeLayer();
  const view = state.project.viewState;
  if (layer?.target?.scope === 'components') return layer;
  if (layer && view.exploded && !view.selected.length) {
    const anchor = layer.anchor?.instanceId || layer.target?.componentInstanceIds?.[0];
    if (!anchor || !state.meshes.has(anchor)) throw new Error('先选择要编辑的部件');
    view.selected = [anchor]; view.selectedQuads = {}; view.paintScope = 'selection';
  }
  if (!layer || view.paintScope === 'assembly' && !view.exploded || !view.selected.length) return layer;
  const ids = [...new Set(view.selected.map(id => state.meshes.get(id)).filter(Boolean)
    .map(mesh => paintMesh(mesh).userData.part).filter(part => applies(layer, part)).map(part => part.instanceId))];
  if (!ids.length) throw new Error('先选择此图层所属的部件');
  const targets = layer.target?.componentInstanceIds || [];
  if (layer.target?.scope === 'components' && targets.length === ids.length && targets.every(id => ids.includes(id))) return layer;
  const local = clone(layer);
  local.layerId = crypto.randomUUID();
  local.target = { scope: 'components', componentInstanceIds: ids, assemblyWeaponIds: [...state.assembly.linkedWeaponIds] };
  for (const key of ['coverage', 'coverageSources']) {
    if (local[key]) local[key] = Object.fromEntries(Object.entries(local[key]).filter(([id]) => ids.includes(id)));
  }
  if (local.imageHash) {
    const oldAnchor = state.meshes.get(layer.anchor?.instanceId);
    if (oldAnchor && layer.anchor.assembledTransform) {
      const delta = oldAnchor.userData.assembled.clone().multiply(new THREE.Matrix4().set(...layer.anchor.assembledTransform.flat()).invert());
      local.projection.origin = new THREE.Vector3(...local.projection.origin).applyMatrix4(delta).toArray();
      const rotation = new THREE.Matrix3().setFromMatrix4(delta);
      for (const axis of ['u', 'v', 'normal']) local.projection[axis] = new THREE.Vector3(...local.projection[axis]).applyMatrix3(rotation).toArray();
    }
    const mesh = state.meshes.get(ids[0]);
    local.anchor = { instanceId: ids[0], localPosition: new THREE.Vector3(...local.projection.origin)
      .applyMatrix4(mesh.userData.assembled.clone().invert()).toArray(), assembledTransform: clone(mesh.userData.part.assembledTransform) };
  }
  layer.target ||= { scope: 'assembly', assemblyWeaponIds: [layer.weaponId] };
  layer.target.excludedComponentInstanceIds = [...new Set([...(layer.target.excludedComponentInstanceIds || []), ...ids])];
  state.project.layers.splice(state.project.layers.indexOf(layer) + 1, 0, local);
  state.layerId = local.layerId;
  return local;
}

function selectLayer(layer) {
  const view = state.project.viewState;
  state.layerId = layer.layerId;
  if (layer.target?.scope === 'components' && !view.selected.some(id => {
    const mesh = state.meshes.get(id);
    return mesh && layer.target.componentInstanceIds.includes(paintMesh(mesh).userData.part.instanceId);
  })) {
    view.selected = layer.target.componentInstanceIds.filter(id => state.meshes.has(id));
    view.selectedQuads = {}; view.paintScope = 'selection';
  } else if ((layer.target?.scope || 'assembly') === 'assembly' && !view.selected.length && !view.exploded) {
    view.paintScope = 'assembly';
  }
  setTool('select');
  showInspector('layers');
  for (const row of $('#layers').children) row.classList.toggle('active', row.dataset.layerId === layer.layerId);
  renderEditor();
}

function activeWeapon() {
  return state.catalog.weapons.find(weapon => weapon.weaponId === state.project.activeWeaponId);
}

function currentWeaponState() {
  const weapon = activeWeapon();
  state.project.weapons[weapon.weaponId] ||= {
    magazine: weapon.defaultMagazine,
    installed: Object.fromEntries(weapon.attachments.map(item => [item.variantId, item.default !== false])),
    includeRelated: false,
    accessories: Object.fromEntries([...weapon.attachments, ...weapon.magazines].filter(item => item.default).map(item => [item.role, item.variantId]))
  };
  const assembly = state.project.weapons[weapon.weaponId];
  assembly.accessories ||= Object.fromEntries([...weapon.attachments, ...weapon.magazines].filter(item => item.default).map(item => [item.role, item.variantId]));
  if (assembly.magazine) assembly.accessories.magazine ||= assembly.magazine;
  return state.project.weapons[weapon.weaponId];
}

function updateHistoryButtons() {
  $('#undo').disabled = state.undo.length === 0;
  $('#redo').disabled = state.redo.length === 0;
}

function scheduleSave() {
  clearTimeout(saveTimer);
  $('#save-state').textContent = '有修改';
  saveTimer = setTimeout(async () => {
    const snapshot = clone(state.project);
    try {
      await api('/api/autosave', { project: snapshot });
      if (snapshot.revision === state.project.revision) $('#save-state').textContent = '已自动保存';
    } catch (error) { handleError(error); }
  }, 650);
}

function commit(change, options = {}) {
  const before = clone(state.project);
  change();
  state.project.revision += 1;
  state.undo.push(before);
  if (state.undo.length > 80) state.undo.shift();
  state.redo = [];
  updateHistoryButtons();
  scheduleSave();
  if (options.assembly) {
    loadAssembly(false).catch(handleError);
  } else if (options.viewOnly) {
    applyVisibility();
    renderComponents();
    renderSelectionGeometry();
  } else if (options.colorOnly) {
    refreshAppearance();
  } else {
    renderLayers();
    renderComponents();
    refreshAppearance();
  }
  if (options.editor !== false) renderEditor();
}

async function restoreHistory(direction) {
  const source = direction === 'undo' ? state.undo : state.redo;
  const destination = direction === 'undo' ? state.redo : state.undo;
  if (!source.length) return;
  destination.push(clone(state.project));
  const revision = state.project.revision + 1;
  state.project = source.pop();
  state.project.revision = revision;
  state.layerId = state.project.layers.some(layer => layer.layerId === state.layerId) ? state.layerId : null;
  state.category = activeWeapon().category;
  renderCatalog();
  await loadAssembly(false);
  renderEditor();
  updateHistoryButtons();
  scheduleSave();
}

// Mesh data stays in unit space. Explosion changes only each display matrix.
const renderer = new THREE.WebGLRenderer({ canvas: $('#scene'), antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.domElement.tabIndex = 0;
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1;
const appearance = new LiveSurfaces(renderer);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(35, 1, .002, 1000);
camera.up.set(0, 0, 1);
camera.position.set(.3, -2.3, .7);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = .10;
controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
const modelRoot = new THREE.Group();
scene.add(modelRoot);
const selections = new THREE.Group();
scene.add(selections);
const hemisphere = new THREE.HemisphereLight(0xf4f5ff, 0x73727d, 2.1);
hemisphere.up.set(0, 0, 1);
hemisphere.position.set(0, 0, 1);
scene.add(hemisphere);
const keyLight = new THREE.DirectionalLight(0xfff6ea, 2.6);
keyLight.position.set(1, -1.5, 2.3);
scene.add(keyLight);
const rimLight = new THREE.DirectionalLight(0xcbd9ff, 1.8);
rimLight.position.set(-1, 1.2, 1);
scene.add(rimLight);
const environmentScene = new THREE.Scene();
environmentScene.background = new THREE.Color('#a5a6b2');
for (const [position, size, intensity] of [
  [[0, 0, 4], [8, 3, .1], 7], [[4, 2, 0], [.1, 6, 4], 3], [[-4, -2, 0], [.1, 5, 5], 1.7]
]) {
  const card = new THREE.Mesh(new THREE.BoxGeometry(...size), new THREE.MeshBasicMaterial({ color: new THREE.Color().setScalar(intensity) }));
  card.position.set(...position);
  environmentScene.add(card);
}
const pmrem = new THREE.PMREMGenerator(renderer);
const environmentTarget = pmrem.fromScene(environmentScene, .08);
scene.environment = environmentTarget.texture;
environmentScene.children.forEach(object => { object.geometry.dispose(); object.material.dispose(); });
pmrem.dispose();
const raycaster = new THREE.Raycaster();
const outlineResolution = new THREE.Vector2();
const mouse = new THREE.Vector2();
let elapsedSeconds = 0;
let lastFrameTime = performance.now();
let displayDistance = 0;
let distanceVelocity = 0;
let distanceAcceleration = 0;
let distanceTransition = null;
let pointerStart = null;

function resize() {
  const bounds = $('#viewport').getBoundingClientRect();
  renderer.setSize(bounds.width, bounds.height, false);
  outlineResolution.set(bounds.width, bounds.height);
  camera.aspect = bounds.width / Math.max(bounds.height, 1);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe($('#viewport'));

function smoothstep(value) {
  return value ** 3 * (value * (6 * value - 15) + 10);
}

function animateDistance(target, immediate = false) {
  if (immediate) {
    displayDistance = target;
    distanceVelocity = 0;
    distanceAcceleration = 0;
    distanceTransition = null;
    return;
  }
  const duration = .6;
  const delta = target - displayDistance;
  const velocity = distanceVelocity * duration;
  const acceleration = distanceAcceleration * duration * duration;
  distanceTransition = {
    start: performance.now(), duration,
    coefficients: [displayDistance, velocity, acceleration / 2,
      10 * delta - 6 * velocity - 1.5 * acceleration,
      -15 * delta + 8 * velocity + 1.5 * acceleration,
      6 * delta - 3 * velocity - .5 * acceleration], target
  };
}

function updateDistance(now) {
  if (!distanceTransition) return;
  const transition = distanceTransition;
  const t = Math.min(1, (now - transition.start) / (transition.duration * 1000));
  const c = transition.coefficients;
  displayDistance = c.reduce((sum, value, index) => sum + value * t ** index, 0);
  distanceVelocity = c.slice(1).reduce((sum, value, index) => sum + (index + 1) * value * t ** index, 0) / transition.duration;
  distanceAcceleration = c.slice(2).reduce((sum, value, index) => sum + (index + 2) * (index + 1) * value * t ** index, 0) / (transition.duration ** 2);
  if (t === 1) {
    displayDistance = transition.target;
    distanceVelocity = 0;
    distanceAcceleration = 0;
    distanceTransition = null;
  }
}

function applyDisplayTransforms(factor = displayDistance) {
  const restored = new Set(state.project?.viewState.restoredParts || []);
  for (const mesh of state.meshes.values()) {
    const matrix = mesh.userData.assembled;
    matrix.decompose(mesh.position, mesh.quaternion, mesh.scale);
    mesh.userData.explosionFactor = restored.has(mesh.userData.part.instanceId) ? 0 : factor;
    mesh.position.addScaledVector(mesh.userData.delta, mesh.userData.explosionFactor);
    mesh.position.add(mesh.userData.workspaceOffset);
    mesh.updateMatrixWorld();
  }
  for (const outline of selections.children) {
    const mesh = state.meshes.get(outline.userData.instanceId);
    if (mesh) outline.matrix.copy(mesh.matrixWorld);
  }
}

function animate(now) {
  requestAnimationFrame(animate);
  elapsedSeconds += Math.max(0, (now - lastFrameTime) / 1000);
  lastFrameTime = now;
  updateDistance(now);
  applyDisplayTransforms();
  if (controls.enabled) controls.update();
  if (state.project) appearance.animate(state.project, state.meshes, elapsedSeconds);
  renderer.render(scene, camera);
  decalEditor.render(displayDistance);
}
requestAnimationFrame(animate);

controls.addEventListener('end', () => {
  if (!state.project) return;
  clearTimeout(cameraTimer);
  cameraTimer = setTimeout(() => {
    state.project.viewState.camera = { position: camera.position.toArray(), target: controls.target.toArray() };
    scheduleSave();
  }, 250);
});

function modelBounds(expanded = true) {
  const bounds = new THREE.Box3();
  for (const mesh of state.meshes.values()) {
    if (!mesh.visible) continue;
    const box = new THREE.Box3().setFromBufferAttribute(mesh.geometry.attributes.position);
    const matrix = mesh.userData.assembled.clone();
    const position = new THREE.Vector3().setFromMatrixPosition(matrix).add(mesh.userData.workspaceOffset);
    if (expanded && !state.project.viewState.restoredParts?.includes(mesh.userData.part.instanceId)) position.add(mesh.userData.delta);
    matrix.setPosition(position);
    bounds.union(box.applyMatrix4(matrix));
  }
  return bounds;
}

function fitView(direction = null, expanded = state.project?.viewState.exploded, record = true) {
  const bounds = modelBounds(expanded);
  if (bounds.isEmpty()) return;
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  const distance = Math.max(size.z, size.x / camera.aspect, size.y * .6) / (2 * tangent) * 1.35;
  const axis = direction || camera.position.clone().sub(controls.target).normalize();
  controls.target.copy(center);
  camera.position.copy(center).addScaledVector(axis, Math.max(distance, .25));
  camera.near = Math.max(.0005, distance / 1000);
  camera.far = Math.max(100, distance * 100);
  camera.updateProjectionMatrix();
  controls.update();
  if (record && state.project) state.project.viewState.camera = { position: camera.position.toArray(), target: controls.target.toArray() };
}

function disposeMaterial(material) {
  material.dispose();
}

function clearModel() {
  appearance.clear();
  state.lastHit = null;
  for (const mesh of state.meshes.values()) {
    mesh.geometry.dispose();
    (Array.isArray(mesh.material) ? mesh.material : [mesh.material]).forEach(disposeMaterial);
    modelRoot.remove(mesh);
  }
  state.meshes.clear();
  clearSelectionGeometry();
}

async function loadAssembly(resetCamera = false) {
  const generation = ++state.loadGeneration;
  $('#loading').hidden = false;
  $('#loading-text').textContent = '载入真实武器模型';
  currentWeaponState();
  const snapshot = clone(state.project);
  const assembly = await api('/api/assembly', { project: snapshot });
  const packHash = Object.values(snapshot.assetLocks)[0];
  await appearance.setPack(packHash);
  const unitIds = [...new Set(assembly.parts.filter(part => part.active && !part.previewOmitted).map(part => part.unitId))];
  const buffers = new Map(await Promise.all(unitIds.map(async id => {
    const response = await fetch(`/models/${packHash}/${id.slice(2)}.bin`);
    if (!response.ok) throw new Error('模型数据读取失败');
    return [id, await response.arrayBuffer()];
  })));
  const native = new Map(await Promise.all(assembly.parts.filter(part => part.active && !part.previewOmitted).map(async part =>
    [part.instanceId, await appearance.nativeMaterials(part)])));
  if (generation !== state.loadGeneration) {
    for (const entry of native.values()) entry.materials.forEach(disposeMaterial);
    return;
  }
  clearModel();
  state.assembly = assembly;
  state.project.links = assembly.links;
  Object.assign(state.project.weapons, assembly.states);
  const activeIds = new Set(assembly.parts.filter(part => part.active && !part.previewOmitted).map(part => part.instanceId));
  state.project.viewState.selected = state.project.viewState.selected.filter(identity => activeIds.has(identity));
  state.project.viewState.restoredParts = (state.project.viewState.restoredParts || []).filter(identity => activeIds.has(identity));
  state.project.viewState.selectedQuads = Object.fromEntries(Object.entries(state.project.viewState.selectedQuads).filter(([identity]) => activeIds.has(identity)));
  for (const part of assembly.parts.filter(part => part.active && !part.previewOmitted)) {
    const source = new Float32Array(buffers.get(part.unitId), part.offset, part.count * 8);
    const positions = new Float32Array(part.count * 3);
    const normals = new Float32Array(part.count * 3);
    const uv = new Float32Array(part.count * 2);
    for (let index = 0; index < part.count; index++) {
      positions.set(source.subarray(index * 8, index * 8 + 3), index * 3);
      normals.set(source.subarray(index * 8 + 3, index * 8 + 6), index * 3);
      uv.set(source.subarray(index * 8 + 6, index * 8 + 8), index * 2);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
    geometry.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
    geometry.computeBoundingSphere();
    const entry = native.get(part.instanceId);
    for (const group of entry.groups) geometry.addGroup(group.start, group.count, group.index);
    const mesh = new THREE.Mesh(geometry, entry.materials);
    mesh.userData = { part, assembled: new THREE.Matrix4().set(...part.assembledTransform.flat()),
      delta: new THREE.Vector3(...part.explosionDelta), workspaceOffset: new THREE.Vector3(...(part.workspaceOffset || [0, 0, 0])) };
    mesh.visible = !(state.project.viewState.hiddenParts || []).includes(part.instanceId);
    modelRoot.add(mesh);
    state.meshes.set(part.instanceId, mesh);
  }
  const sample = assembly.parts.find(part => part.active && part.displayAmmoSample);
  if (sample) {
    for (const layer of [...(state.project.colors || []), ...state.project.layers]) {
      const updated = rebindAmmoLayer(layer, sample);
      if (updated !== layer) Object.assign(layer, updated);
    }
  }
  let upgradedDepth = false;
  for (const layer of state.project.layers) {
    if (!layer.imageHash || layer.projection.type === 'cylinder' || layer.projection.depthMode === 'inward') continue;
    const bounds = layerBounds(layer);
    if (bounds.isEmpty()) continue;
    layer.projection = frontPlaneProjection(layer.projection, bounds, true);
    upgradedDepth = true;
  }
  if (upgradedDepth) { state.project.revision += 1; scheduleSave(); }
  animateDistance(smoothstep(state.project.viewState.exploded ? state.project.viewState.distance : 0), true);
  applyDisplayTransforms();
  $('#weapon-title').textContent = assembly.weapon.label;
  $('#weapon-native').textContent = assembly.weapon.unitId;
  $('#project-name').value = state.project.name;
  $('#mesh-info').textContent = assembly.linkedWeaponIds.length > 1 ? assembly.linkedWeaponIds.map(id => state.catalog.weapons.find(item => item.weaponId === id).label).join('  ＋  ') : `${state.meshes.size} 个部件`;
  if (resetCamera) {
    const saved = state.project.viewState.camera;
    if (saved) { camera.position.fromArray(saved.position); controls.target.fromArray(saved.target); }
    else fitView(new THREE.Vector3(.15, -1, .14).normalize());
  }
  renderAssemblyControls();
  renderLayers();
  renderComponents();
  renderSelectionGeometry();
  setEnvironment();
  temporaryBrush = null;
  setTool('select');
  $('#loading').hidden = true;
  $('#status').textContent = '工作台已就绪';
  window.__studioReady = true;
  await refreshAppearance();
  await appearance.prune(state.meshes);
}

function renderCatalog() {
  $('#categories').replaceChildren();
  for (const [id, label] of Object.entries(state.catalog.categories)) {
    const button = document.createElement('button');
    button.textContent = label;
    button.role = 'tab';
    button.classList.toggle('active', id === state.category);
    button.setAttribute('aria-selected', String(id === state.category));
    button.addEventListener('click', () => { state.category = id; renderCatalog(); });
    $('#categories').append(button);
  }
  const search = $('#search').value.toLocaleLowerCase().trim();
  const weapons = state.catalog.weapons.filter(weapon => weapon.category === state.category &&
    (weapon.label + weapon.englishName + weapon.unitId).toLocaleLowerCase().includes(search));
  $('#weapon-list').replaceChildren();
  for (const weapon of weapons) {
    const row = document.createElement('button');
    row.className = 'weapon-row';
    row.classList.toggle('active', weapon.weaponId === state.project.activeWeaponId);
    row.classList.toggle('unavailable', weapon.status !== 'ready');
    row.dataset.weaponId = weapon.weaponId;
    const duplicate = state.catalog.weapons.filter(item => item.label === weapon.label).length > 1;
    const subtitle = weapon.status === 'ready' ? (duplicate ? '资源 ' + weapon.unitId.slice(-6) : '') : weapon.reason;
    row.innerHTML = `<span class="weapon-name">${escapeHTML(weapon.label)}</span>${subtitle ? `<span class="weapon-code">${escapeHTML(subtitle)}</span>` : ''}`;
    row.title = weapon.nativePath + '\n' + weapon.entityId;
    row.addEventListener('click', async () => {
      if (weapon.status !== 'ready') { toast(weapon.reason, true); return; }
      if (weapon.weaponId === state.project.activeWeaponId) return;
      try {
        commit(() => {
          state.project.activeWeaponId = weapon.weaponId;
          state.project.viewState.selected = [];
          state.project.viewState.selectedQuads = {};
          state.project.viewState.paintScope = 'selection';
          state.project.viewState.camera = null;
          state.layerId = null;
        }, { editor: false });
        renderCatalog();
        await loadAssembly(true);
        renderEditor();
      } catch (error) { handleError(error); }
    });
    $('#weapon-list').append(row);
  }
  $('#catalog-count').textContent = weapons.length + ' 件';
}

function renderAssemblyControls() {
  $('#attachments').replaceChildren();
  const roles = { optic: '瞄具', magazine: '弹匣与装填件', muzzle: '枪口', underbarrel: '下挂配件' };
  for (const identity of state.assembly.linkedWeaponIds) {
    const host = state.catalog.weapons.find(item => item.weaponId === identity);
    const assembly = state.project.weapons[identity];
    const items = [...host.attachments, ...host.magazines].filter(item => host.category !== 'support' || item.default);
    if (state.assembly.linkedWeaponIds.length > 1 && items.length) {
      const title = document.createElement('p');
      title.className = 'attachment-host';
      title.textContent = host.label;
      $('#attachments').append(title);
    }
    for (const [role, defaultTitle] of Object.entries(roles)) {
      const variants = items.filter(item => item.role === role);
      if (!variants.length) continue;
      const chosen = variants.find(item => item.variantId === assembly.accessories[role]);
      const paired = Boolean(chosen?.secondary);
      const battery = paired && chosen.label.includes('散热器');
      const title = paired ? (battery ? '第一散热器' : '第一装填件') : defaultTitle;
      const label = document.createElement('label');
      label.className = 'field';
      label.textContent = title;
      if (variants.length === 1) {
        label.className = 'fixed-attachment';
        const value = paired ? accessoryFocusButton(identity, chosen.variantId, chosen.label)
          : document.createElement('strong');
        if (!paired) value.textContent = variants[0].label.replace('（原装）', '');
        label.append(value);
        $('#attachments').append(label);
      } else {
        const select = document.createElement('select');
        select.dataset.attachmentRole = role;
        select.dataset.hostId = identity;
        select.setAttribute('aria-label', title);
        for (const item of variants) {
          const option = new Option(item.label + (item.unavailable ? '（缺少模型）' : ''), item.variantId);
          option.disabled = Boolean(item.unavailable);
          if (item.unavailable) option.title = item.unavailable;
          select.add(option);
        }
        select.value = assembly.accessories[role];
        select.addEventListener('change', () => commit(() => {
          assembly.accessories[role] = select.value;
          if (role === 'magazine') assembly.magazine = select.value;
          state.project.viewState.selectedQuads = {};
          state.project.viewState.paintScope = 'selection';
        }, { assembly: true }));
        label.append(select);
        $('#attachments').append(label);
      }
      if (paired) {
        const secondary = document.createElement('div');
        secondary.className = 'fixed-attachment';
        secondary.textContent = battery ? '第二散热器' : '第二装填件';
        secondary.append(accessoryFocusButton(identity, chosen.variantId + '_secondary', chosen.secondary.label || chosen.label));
        $('#attachments').append(secondary);
      }
    }
  }
  if (!$('#attachments').children.length) $('#attachments').innerHTML = '<p class="empty">此装备没有独立配件。</p>';

}

function accessoryFocusButton(host, variant, label) {
  const button = document.createElement('button');
  button.textContent = label.replace('（原装）', '');
  const part = state.assembly.parts.find(item => item.active && item.hostEntityId === host &&
    item.variantId === variant && item.role !== 'ammo');
  if (part) {
    button.dataset.accessoryInstance = part.instanceId;
    button.addEventListener('click', () => {
      const row = [...document.querySelectorAll('#components [data-instance-id]')]
        .find(item => item.dataset.instanceId === part.instanceId);
      row?.click();
    });
  }
  return button;
}

function applyVisibility() {
  const hidden = new Set(state.project.viewState.hiddenParts || []);
  for (const [identity, mesh] of state.meshes) mesh.visible = !hidden.has(identity);
}

function renderComponents() {
  if (!state.assembly) return;
  const selected = state.project.viewState.selected;
  $('#components').replaceChildren();
  const all = document.createElement('button');
  all.textContent = '选择整件装配';
  all.disabled = Boolean(state.project.viewState.exploded);
  all.classList.toggle('active', state.project.viewState.paintScope === 'assembly');
  all.addEventListener('click', () => {
    state.layerId = null;
    state.project.viewState.selected = [];
    state.project.viewState.selectedQuads = {};
    state.project.viewState.paintScope = 'assembly';
    renderComponents(); renderLayers(); renderEditor(); renderSelectionGeometry(); scheduleSave();
  });
  $('#components').append(all);
  let previousHost;
  for (const part of state.assembly.parts.filter(part => part.active && !part.previewOmitted)) {
    if (state.assembly.linkedWeaponIds.length > 1 && part.hostEntityId !== previousHost) {
      const title = document.createElement('p');
      title.className = 'component-host';
      title.textContent = part.hostLabel;
      $('#components').append(title);
      previousHost = part.hostEntityId;
    }
    const row = document.createElement('div');
    row.className = 'component-row';
    const hidden = (state.project.viewState.hiddenParts || []).includes(part.instanceId);
    row.classList.toggle('is-hidden', hidden);
    const button = document.createElement('button');
    button.className = 'component-select';
    button.innerHTML = `<span>${escapeHTML(part.label)}</span>${part.role === 'ammo' || part.paintSourceId ? '<small>涂装同步</small>' : ''}`;
    button.dataset.instanceId = part.instanceId;
    button.classList.toggle('active', selected.includes(part.instanceId));
    button.addEventListener('click', e => {
      state.layerId = null;
      state.project.viewState.paintScope = 'selection';
      state.lastHit = null;
      state.project.viewState.hiddenParts = (state.project.viewState.hiddenParts || []).filter(id => id !== part.instanceId);
      applyVisibility();
      state.project.viewState.selected = e.ctrlKey || e.shiftKey ?
        selected.includes(part.instanceId) ? selected.filter(id => id !== part.instanceId) : [...selected, part.instanceId] : [part.instanceId];
      state.project.viewState.selectedQuads = {};
      renderComponents(); renderLayers(); renderEditor(); renderSelectionGeometry(); scheduleSave();
    });
    const visibility = document.createElement('button');
    visibility.className = 'visibility-button';
    visibility.textContent = hidden ? '显示' : '隐藏';
    visibility.setAttribute('aria-label', (hidden ? '显示' : '隐藏') + part.label);
    visibility.dataset.visibilityId = part.instanceId;
    visibility.addEventListener('click', () => commit(() => {
      const identities = new Set(state.project.viewState.hiddenParts || []);
      if (hidden) identities.delete(part.instanceId);
      else identities.add(part.instanceId);
      state.project.viewState.hiddenParts = [...identities];
    }, { viewOnly: true, editor: false }));
    row.append(button, visibility);
    $('#components').append(row);
  }
  const count = Object.values(state.project.viewState.selectedQuads || {}).reduce((sum, values) => sum + values.length, 0);
  const whole = state.project.viewState.paintScope === 'assembly';
  $('#paint-scope').value = whole ? 'assembly' : 'selection';
  const available = whole || selected.some(id => state.meshes.has(id));
  for (const id of ['import-image', 'import-rear', 'import-cylinder', 'add-text', 'add-emission']) $('#' + id).disabled = !available;
  $('#paint-color').disabled = $('#paint-hex').disabled = !available && state.tool !== 'paint';
  $('#paint-scope option[value=assembly]').disabled = Boolean(state.project.viewState.exploded);
  $('#restore-color').disabled = !selected.length || count > 0 || whole || !available;
  $('#restore-color').textContent = '还原部件';
  $('#restore-color').title = '选择完整部件后恢复该部件配色';
  const selectedParts = selected.filter(id => state.meshes.has(id));
  $('#restore-selected').disabled = !state.project.viewState.exploded || !selectedParts.length;
  $('#restore-selected').textContent = selectedParts.length && selectedParts.every(id => state.project.viewState.restoredParts?.includes(id)) ? '拆开所选' : '归位所选';
  $('#explode').textContent = state.project.viewState.exploded ? '装配全部' : '拆解部件';
  renderPaintPanel();
}

function targetForNewLayer() {
  const selected = state.project.viewState.selected.map(id => state.meshes.get(id)).filter(Boolean);
  const ids = [...new Set(selected.map(mesh => paintMesh(mesh).userData.part.instanceId))];
  const whole = state.project.viewState.paintScope === 'assembly' && !state.project.viewState.exploded;
  if (!whole && !ids.length) throw new Error('请先选择部件');
  const ammoParts = whole ? [] : ids.map(id => state.meshes.get(id).userData.part).filter(part => part.role === 'ammo');
  const ammo = ammoParts[0];
  const target = { scope: whole ? 'assembly' : 'components', componentInstanceIds: whole ? [] : ids,
    assemblyWeaponIds: [state.project.activeWeaponId] };
  let ammoSource;
  if (ammo) {
    target.ammoHosts = [...new Set(ammoParts.map(part => part.hostEntityId))];
    const mesh = state.meshes.get(ammo.instanceId);
    const bounds = new THREE.Box3().setFromBufferAttribute(mesh.geometry.attributes.position);
    ammoSource = { unitId: ammo.unitId, componentId: ammo.componentId, instanceId: ammo.instanceId,
      bounds: [bounds.min.toArray(), bounds.max.toArray()], assembledTransform: clone(ammo.assembledTransform) };
  }
  return { target, coverageMode: 'whole', coverage: {}, coverageSources: {}, ...(ammoSource ? { ammoSource } : {}) };
}

function newLayer(kind, extra = {}) {
  const layer = { layerId: crypto.randomUUID(), weaponId: state.project.activeWeaponId, kind,
    name: kind === 'fill' ? '颜色填充' : kind === 'emission' ? '发光' : '图片', enabled: true,
    opacity: 1, color: $('#paint-color').value, roughness: .65, metalness: .2,
    ...(extra.target ? {} : targetForNewLayer()), ...extra };
  commit(() => { state.project.layers.push(layer); state.layerId = layer.layerId; });
  return layer;
}

function layerTargetLabel(layer) {
  if (isArtwork(layer) && layer.target?.scope === 'assembly') return '自由贴花';
  if (layer.target?.scope === 'assembly') return '整体装配';
  const ids = layer.target?.componentInstanceIds || [];
  const labels = ids.map(id => state.assembly.parts.find(part => part.instanceId === id)?.label ||
    (layer.migrationPending ? '待重新绑定部件' : '未装配部件'));
  const count = Object.values(layer.coverage || {}).reduce((sum, values) => sum + values.length, 0);
  return labels.join('、') + (layer.coverageMode === 'quads' ? ' · 历史局部涂装' : '');
}

function layerInWorkspace(layer) {
  const hosts = state.assembly.linkedWeaponIds;
  if (!layer.target || (layer.target.scope || 'assembly') === 'assembly') {
    return (layer.target?.assemblyWeaponIds || [layer.weaponId]).some(identity => hosts.includes(identity));
  }
  return (layer.target?.componentInstanceIds || []).some(identity => hosts.includes(identity.split('/')[0]));
}

function renderLayers() {
  const layers = state.project.layers.filter(layerInWorkspace);
  $('#layers').replaceChildren();
  if (!layers.length) $('#layers').innerHTML = '<p class="empty">点击“添加贴花”，或把图片拖到武器上。</p>';
  for (const layer of [...layers].reverse()) {
    const row = document.createElement('div');
    row.className = 'layer-row';
    row.classList.toggle('active', layer.layerId === state.layerId);
    row.dataset.layerId = layer.layerId;
    row.draggable = true;
    const effect = layer.kind === 'emission' || layer.emission?.enabled ? LIGHT_MODES[layer.emission?.mode || 'static'] : '';
    const description = [layerTargetLabel(layer), effect].filter(Boolean).join(' · ');
    row.innerHTML = `<span class="layer-icon">${layer.imageHash ? `<img src="/images/${layer.imageHash}.png" alt="">` : layer.kind === 'emission' ? '✦' : '■'}</span><span class="layer-info"><strong class="layer-name" title="双击重命名">${escapeHTML(layer.name)}</strong><small>${escapeHTML(description)}</small></span><button class="layer-visible" aria-label="${layer.enabled ? '隐藏图层' : '显示图层'}"><span class="visibility-dot ${layer.enabled !== false ? 'is-visible' : ''}"></span></button>`;
    if (!layer.imageHash) row.querySelector('.layer-icon').style.color = layer.emission?.color || layer.color;
    row.addEventListener('click', event => {
      if (event.target.closest('input')) return;
      selectLayer(layer);
    });
    row.querySelector('.layer-name').addEventListener('dblclick', event => { event.stopPropagation(); renameLayer(layer); });
    row.querySelector('.layer-visible').addEventListener('click', e => { e.stopPropagation(); commit(() => { layer.enabled = !layer.enabled; }); });
    const remove = document.createElement('button');
    remove.className = 'layer-delete'; remove.textContent = '×'; remove.setAttribute('aria-label', '删除图层 ' + layer.name);
    remove.addEventListener('click', event => { event.stopPropagation(); state.layerId = layer.layerId; deleteLayer(); });
    row.append(remove);
    row.addEventListener('contextmenu', event => {
      event.preventDefault();
      event.stopPropagation();
      selectLayer(layer);
      openContextMenu(event, [
        ...(isArtwork(layer) ? [
        { label: '裁剪图片', action: () => { decalEditor.crop = true; } },
        { label: oppositeSideLabel(layer), action: copyToOppositeSide },
        { label: '绑定当前部件', disabled: !state.project.viewState.selected.length && state.project.viewState.paintScope !== 'assembly', action: () => {
          const target = targetForNewLayer();
          commit(() => { Object.assign(layer, target); delete layer.migrationPending; });
        } },
        { label: '贴合另一个表面', action: () => setTool('place') },
        ] : []),
        { label: '复制图层', action: duplicateLayer },
        { label: '上移', action: () => moveLayer(layer, 1) },
        { label: '下移', action: () => moveLayer(layer, -1) },
        { label: '重命名', action: () => renameLayer(layer) },
        null,
        { label: '删除图层', action: deleteLayer, danger: true }
      ]);
    });
    row.addEventListener('dragstart', e => e.dataTransfer.setData('text/plain', layer.layerId));
    row.addEventListener('dragover', e => e.preventDefault());
    row.addEventListener('drop', e => {
      e.preventDefault();
      const identity = e.dataTransfer.getData('text/plain');
      if (identity === layer.layerId) return;
      commit(() => {
        const index = state.project.layers.findIndex(item => item.layerId === identity);
        if (index < 0) return;
        const [moved] = state.project.layers.splice(index, 1);
        const destination = state.project.layers.indexOf(layer);
        state.project.layers.splice(destination, 0, moved);
      });
    });
    $('#layers').append(row);
  }
}

function renameLayer(layer) {
  const row = [...$('#layers').children].find(item => item.dataset.layerId === layer.layerId);
  const name = row?.querySelector('.layer-name');
  if (!name || name.querySelector('input')) return;
  const input = document.createElement('input');
  input.className = 'layer-rename'; input.value = layer.name; input.maxLength = 120;
  input.setAttribute('aria-label', '图层名称');
  row.draggable = false;
  name.replaceChildren(input);
  let finished = false;
  const finish = save => {
    if (finished) return;
    finished = true;
    const value = input.value.trim();
    if (save && value && value !== layer.name) commit(() => { layer.name = value; });
    else renderLayers();
  };
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.key === 'Enter' || event.key === 'Escape') { event.preventDefault(); finish(event.key === 'Enter'); }
  });
  input.focus(); input.select();
}

function renderEditor() {
  const layer = activeLayer();
  const imageLight = isArtwork(layer);
  document.querySelector('[data-tool="select"]').classList.toggle('active', state.tool === 'select' && !imageLight);
  if (imageLight || layer?.kind === 'emission') clearSelectionGeometry();
  $('#layer-properties').hidden = isBrushTool(state.tool) || !imageLight && layer?.kind !== 'emission';
  $('#layer-property-name').textContent = layer?.name || '';
  $('#layer-property-target').textContent = layer ? layerTargetLabel(layer) : '';
  $('#decal-properties').hidden = !imageLight;
  renderLightEditor();
  if (imageLight) {
    const cylinder = layer.projection.type === 'cylinder';
    const side = layer.projection.side || planeSide(layer.projection.normal);
    const end = isEndSide(side);
    $('#copy-to-back').textContent = oppositeSideLabel(layer);
    $('#decal-side').closest('label').hidden = cylinder;
    for (const option of $('#decal-side').options) {
      option.hidden = option.value === 'view' ? side !== 'view' : isEndSide(option.value) !== end;
    }
    $('#decal-side').value = side;
    $('#decal-projection').value = cylinder ? 'cylinder' : end ? 'end' : 'plane';
    $('#cylinder-settings').hidden = !cylinder;
    $('#decal-axis').value = String(layer.projection.axis ?? 'auto');
    $('#decal-angle').value = Number((layer.placement.angle || 0).toFixed(2));
    $('#decal-scale').value = Number((layer.placement.scale || 1).toFixed(3));
    $('#decal-x').value = Number(((layer.placement.x || 0) * 1000).toFixed(3));
    $('#decal-y').value = Number(((layer.placement.y || 0) * 1000).toFixed(3));
    if (cylinder) {
      const scale = layer.placement.scale || 1;
      $('#decal-wrap').value = Math.min(360, Number(THREE.MathUtils.radToDeg(layer.projection.width * scale / layer.projection.radius).toFixed(1)));
      $('#decal-wrap-height').value = Number((layer.projection.height * scale * 1000).toFixed(2));
    }
  }
}

function applies(layer, part) {
  return appearance.applies(layer, part);
}

function refreshAppearance() {
  if (!state.assembly) return Promise.resolve();
  const pending = appearance.refresh(state.project, state.meshes);
  pending.catch(handleError);
  return pending;
}

function renderPaintPanel() {
  const container = $('#components');
  for (const part of state.assembly.parts.filter(part => part.active && !part.previewOmitted)) {
    const row = [...container.querySelectorAll('.component-row')].find(item => item.querySelector('[data-instance-id]')?.dataset.instanceId === part.instanceId);
    if (!row) continue;
    row.querySelector('.color-choice')?.remove();
    row.dataset.colorPart = part.instanceId;
    row.classList.toggle('active', state.project.viewState.selected.includes(part.instanceId));
    const palette = document.createElement('label');
    palette.className = 'color-choice';
    const latest = [...(state.project.colors || [])].reverse().find(item => item.coverageMode !== 'quads' && applies(item, part));
    const current = latest?.color || '#808080';
    palette.innerHTML = `<input type="color" value="${current}" aria-label="${escapeHTML(part.label)}颜色"><input class="part-hex" value="${latest?.color || ''}" placeholder="原材质" maxlength="7" aria-label="${escapeHTML(part.label)}色值">`;
    const color = palette.querySelector('[type=color]'), hex = palette.querySelector('.part-hex');
    const paintTarget = part.paintSourceId || part.instanceId;
    const target = { target: { scope: 'components', componentInstanceIds: [paintTarget], assemblyWeaponIds: [...state.assembly.linkedWeaponIds],
      ...(part.role === 'ammo' ? { ammoHosts: [part.hostEntityId] } : {}) }, coverageMode: 'whole', coverage: {}, coverageSources: {} };
    const selectPart = () => {
      state.project.viewState.selected = [part.instanceId]; state.project.viewState.selectedQuads = {};
      state.project.viewState.paintScope = 'selection'; state.layerId = null;
      for (const item of container.children) item.classList.toggle('active', item.dataset.colorPart === part.instanceId);
      renderEditor(); renderSelectionGeometry();
    };
    color.addEventListener('input', () => { selectPart(); hex.value = color.value; previewColor(target, color.value); });
    color.addEventListener('change', () => { finishColorGesture(); renderComponents(); });
    hex.addEventListener('change', () => {
      if (!/^#[0-9a-f]{6}$/i.test(hex.value)) { hex.value = latest?.color || ''; toast('请输入六位十六进制颜色', true); return; }
      selectPart(); color.value = hex.value; applyColor(target, hex.value); renderComponents();
    });
    row.insertBefore(palette, row.querySelector('.visibility-button'));
  }
}

function writeColor(target, color) {
  const key = JSON.stringify([target.target, target.coverageMode, target.coverage]);
  state.project.colors ||= [];
  const index = state.project.colors.findIndex(item => JSON.stringify([item.target, item.coverageMode, item.coverage]) === key);
  const existing = index >= 0 ? state.project.colors.splice(index, 1)[0] : { layerId: crypto.randomUUID(), weaponId: state.project.activeWeaponId, kind: 'fill', name: '部件配色', enabled: true, opacity: 1, roughness: .65, metalness: .2 };
  state.project.colors.push({ ...existing, ...target, color });
  state.layerId = null;
}

function applyColor(target = targetForNewLayer(), color = $('#paint-color').value) {
  commit(() => writeColor(target, color), { colorOnly: true });
  renderPaintPanel();
  $('#status').textContent = '颜色已应用到所选目标';
}

let colorBefore = null;
function previewColor(target, color) {
  colorBefore ||= clone(state.project);
  writeColor(target, color);
  refreshAppearance();
}

function finishColorGesture() {
  if (!colorBefore) return;
  state.undo.push(colorBefore); colorBefore = null;
  if (state.undo.length > 80) state.undo.shift();
  state.redo = []; state.project.revision += 1;
  updateHistoryButtons(); scheduleSave(); renderEditor();
  renderPaintPanel();
}

function showInspector(name) {
  for (const button of document.querySelectorAll('[data-inspector]')) {
    const active = button.dataset.inspector === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  }
  for (const id of ['layers', 'materials']) $('#' + id + '-panel').hidden = id !== name;
}

function lightSettings(layer) {
  const baseline = isArtwork(layer) ? { color: '#ffffff', intensity: 2, maximum: 2 } : { color: layer.color || DEFAULT_LIGHT.color };
  return { ...DEFAULT_LIGHT, ...baseline, ...layer.emission };
}

const lightControls = $('#light-template').content.firstElementChild.cloneNode(true);
$('#layer-light-host').append(lightControls);

const lightFields = {
  color: 'emission-color', intensity: 'emission-strength', mode: 'emission-effect',
  minimum: 'light-minimum', maximum: 'light-maximum', period: 'light-period',
  speed: 'light-speed', width: 'light-width', direction: 'light-direction'
};

function renderLightEditor() {
  if (!state.project) return;
  const image = $('#mask-dialog').open;
  const layer = activeLayer();
  const inline = layer?.kind === 'emission' && !image && !isBrushTool(state.tool);
  const host = $(image ? '#image-light-host' : '#layer-light-host');
  if (lightControls.parentElement !== host) host.append(lightControls);
  $('#layer-light-host').hidden = !inline;
  if (!image && !inline) return;
  const light = image ? lightRegions.settings : lightSettings(layer);
  if (image) $('#emission-enabled').checked = light.enabled;
  lightControls.hidden = image && !light.enabled;
  $('#light-color-field').hidden = image;
  $('#light-strength-field').hidden = light.mode === 'breathe';
  $('#breathe-controls').hidden = light.mode !== 'breathe';
  $('#flow-controls').hidden = light.mode !== 'flow';
  for (const [key, id] of Object.entries(lightFields)) {
    $('#' + id).value = key === 'width' ? Math.round(light[key] * 10000) / 100 : light[key];
  }
}

function setFlowRange(light, bounds) {
  if (light.mode !== 'flow') return light;
  if (bounds.isEmpty()) throw new Error('请先选择流光覆盖的部件');
  return { ...light, range: [bounds.min.x, Math.max(bounds.min.x + .0001, bounds.max.x)] };
}

function writeLightControl(key, input) {
  const image = $('#mask-dialog').open;
  const layer = image ? state.project.layers.find(item => item.layerId === lightRegions.layerId) : activeLayer();
  if (!layer) return;
  const light = image ? { ...lightRegions.settings } : lightSettings(layer);
  if (input.type === 'number' && (!input.value || !input.checkValidity())) {
    renderLightEditor();
    throw new Error('发光参数超出输入范围');
  }
  const value = input.type === 'number' ? input.valueAsNumber : input.value;
  light[key] = key === 'width' ? value / 100 : value;
  if (key === 'minimum') light.maximum = Math.max(light.minimum, light.maximum);
  if (key === 'maximum') light.minimum = Math.min(light.minimum, light.maximum);
  const updated = light.mode === 'flow' && !light.range ? setFlowRange(light, layerBounds(layer)) : light;
  if (image) {
    lightRegions.settings = updated;
    renderLightEditor();
  } else commit(() => { layer.emission = updated; });
}

function setEnvironment() {
  hemisphere.intensity = 2.1;
  keyLight.intensity = 2.6;
  rimLight.intensity = 1.8;
  scene.environmentIntensity = .8;
}

function trianglePoints(mesh, triangle) {
  const array = mesh.geometry.attributes.position.array;
  return [0, 1, 2].map(corner => new THREE.Vector3().fromArray(array, triangle * 9 + corner * 3));
}

function quadPoints(mesh, quad) {
  const [triangle, other, corner, otherCorner] = quad;
  const points = trianglePoints(mesh, triangle);
  if (other >= 0) {
    return [points[corner], points[(corner + 1) % 3], trianglePoints(mesh, other)[otherCorner], points[(corner + 2) % 3]];
  }
  const a = points[corner];
  return [a, a.clone().add(points[(corner + 1) % 3]).multiplyScalar(.5),
    points[0].clone().add(points[1]).add(points[2]).multiplyScalar(1 / 3),
    a.clone().add(points[(corner + 2) % 3]).multiplyScalar(.5)];
}

function clearSelectionGeometry() {
  for (const outline of [...selections.children]) { outline.geometry.dispose(); outline.material.dispose(); selections.remove(outline); }
}

function renderSelectionGeometry() {
  clearSelectionGeometry();
  const view = state.project.viewState;
  if (activeDecal() || activeLayer()?.kind === 'emission' || isBrushTool(state.tool)) return;
  for (const [identity, mesh] of state.meshes) {
    if (!mesh.visible) continue;
    const selected = view.selectedQuads?.[identity] || [];
    const whole = view.paintScope === 'assembly' || view.selected.includes(identity) && !selected.length;
    if (!selected.length && !whole) continue;
    const mask = { coverageMode: whole ? 'whole' : 'quads', coverage: { [identity]: selected } };
    const geometry = appearance.geometry(mesh, mask);
    const fillGeometry = geometry === mesh.geometry ? geometry.clone() : geometry;
    const fill = new THREE.Mesh(fillGeometry, new THREE.MeshBasicMaterial({ color: '#3474ff', transparent: true,
      opacity: .23, side: THREE.DoubleSide, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -3 }));
    fill.matrixAutoUpdate = false; fill.userData.instanceId = identity; fill.renderOrder = 9; selections.add(fill);
    for (const [index, outline] of selectionOutlines(fillGeometry, whole, outlineResolution).entries()) {
      outline.matrixAutoUpdate = false; outline.userData.instanceId = identity;
      outline.renderOrder = 10 + index; selections.add(outline);
    }
  }
  applyDisplayTransforms();
}

function setTool(tool) {
  tool = ['place', 'paint', 'lightPaint'].includes(tool) ? tool : 'select';
  if (tool !== state.tool) finishBrush();
  state.tool = tool;
  if (state.project) state.project.viewState.mode = tool;
  $('#brush-controls').hidden = !isBrushTool(tool);
  $('#paint-color').closest('label').hidden = tool === 'lightPaint';
  $('#brush-light-color').hidden = tool !== 'lightPaint';
  if (isBrushTool(tool)) $('#brush-shape').value = state.brushShapes[tool];
  if (!isBrushTool(tool)) $('#brush-cursor').hidden = true;
  decalEditor.interactive = tool === 'select';
  controls.enableRotate = true;
  document.querySelectorAll('[data-tool]').forEach(button => button.classList.toggle('active', button.dataset.tool === tool && !activeDecal()));
  $('[data-tool=paint]').setAttribute('aria-pressed', String(tool === 'paint'));
  $('#paint-emission').setAttribute('aria-pressed', String(tool === 'lightPaint'));
  $('#scene').style.cursor = tool === 'select' ? 'grab' : 'crosshair';
  renderToolHint();
  renderComponents();
  renderEditor();
  renderSelectionGeometry();
}

function renderToolHint() {
  const shape = state.brushShapes[state.tool];
  const hint = { freehand: '拖动绘制', line: '从起点拖到终点', curve: '拖出弧线 · Shift 切换弯曲方向',
    circle: '从圆心向外拖出半径' }[shape];
  $('#viewport-hint').textContent = state.tool === 'place' ? '单击目标部件的表面贴合图片' :
    hint ? hint + ' · Alt＋拖动旋转' : '拖动旋转 · 单击选择部件 · Ctrl 多选 · 按住 B 临时画笔';
}

function hitTest(event) {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.set((event.clientX - rect.left) / rect.width * 2 - 1, 1 - (event.clientY - rect.top) / rect.height * 2);
  raycaster.setFromCamera(mouse, camera);
  return raycaster.intersectObjects([...state.meshes.values()].filter(mesh => mesh.visible), false)[0];
}

function paintMesh(mesh) {
  return state.meshes.get(mesh.userData.part.paintSourceId) || mesh;
}

function selectHit(hit, event) {
  const part = hit.object.userData.part;
  state.lastHit = hit;
  state.project.viewState.paintScope = 'selection';
  state.layerId = null;
  const previous = state.project.viewState.selected;
  state.project.viewState.selected = event.shiftKey || event.ctrlKey
    ? previous.includes(part.instanceId) ? previous.filter(id => id !== part.instanceId) : [...previous, part.instanceId]
    : [part.instanceId];
  state.project.viewState.selectedQuads = {};
  renderComponents(); renderLayers(); renderEditor(); renderSelectionGeometry(); scheduleSave();
}

function placeHit(hit) {
  const layer = activeLayer();
  if (!layer?.imageHash) { toast('请先选择一个贴花图层'); return; }
  const mesh = paintMesh(hit.object);
  const part = mesh.userData.part;
  if (!applies(layer, part)) { toast('请在此贴花所属的部件内选择表面'); return; }
  commit(() => {
    const editable = editableLayer(), projection = editable.projection;
    const local = hit.object.worldToLocal(hit.point.clone());
    const point = local.clone().applyMatrix4(mesh.userData.assembled);
    if (projection.type === 'cylinder') {
      const frame = decalEditor.frame(editable, false);
      const delta = point.sub(frame.axisOrigin);
      editable.placement.x = projection.radius * Math.atan2(delta.dot(frame.radialU), delta.dot(frame.radialNormal));
      editable.placement.y = delta.dot(frame.v);
      return;
    }
    editable.projection = frontPlaneProjection({ ...projection, origin: point.toArray() }, layerBounds(editable), true);
    editable.anchor = { instanceId: part.instanceId, triangle: hit.faceIndex, localPosition: local.toArray(),
      assembledTransform: clone(part.assembledTransform) };
    editable.placement.x = 0;
    editable.placement.y = 0;
  });
  setTool('select');
}

const decalEditor = new DecalEditor({ canvas: renderer.domElement, viewport: $('#viewport'), camera, controls,
  layer: activeDecal, meshes: () => state.meshes, applies,
  displayAnchor: identity => state.project.viewState.selected.map(id => state.meshes.get(id))
    .find(mesh => mesh?.userData.part.paintSourceId === identity),
  begin: () => {
    gestureBefore = clone(state.project);
    state.gestureLayerId = state.layerId;
    try { editableLayer(); } catch (error) { gestureBefore = null; handleError(error); return false; }
    closeContextMenu();
  },
  change: () => { refreshAppearance(); renderSelectionGeometry(); },
  end: changed => {
    if (changed && gestureBefore) {
      state.undo.push(gestureBefore); if (state.undo.length > 80) state.undo.shift();
      state.redo = []; state.project.revision += 1;
      updateHistoryButtons(); scheduleSave(); renderLayers(); renderEditor();
    } else if (gestureBefore && state.layerId !== state.gestureLayerId) {
      state.project = gestureBefore; state.layerId = state.gestureLayerId;
      renderLayers(); refreshAppearance();
    }
    gestureBefore = null;
  }
});
let lastPointer = null;
let paintStroke = null;
let temporaryBrush = null;

function paintSurface(event) {
  const hit = hitTest(event);
  if (!hit) return;
  const mesh = paintMesh(hit.object), part = mesh.userData.part;
  if (paintStroke.ids.length && !paintStroke.ids.includes(part.instanceId)) return;
  if (paintStroke.lockFirst) {
    paintStroke.ids = [part.instanceId];
    paintStroke.lockFirst = false;
  }
  const position = hit.object.worldToLocal(hit.point.clone());
  const radius = THREE.MathUtils.clamp(Number($('#brush-size').value) || 8, .5, 60) / 1000;
  const previous = paintStroke.layer?.brush.stamps.at(-1);
  if (previous?.partId === part.instanceId && new THREE.Vector3(...previous.position).distanceTo(position) < radius * .3) return;
  if (!paintStroke.layer || paintStroke.layer.brush.stamps.length >= 4096) {
    const light = paintStroke.light;
    const layer = { layerId: crypto.randomUUID(), weaponId: state.project.activeWeaponId,
      kind: light ? 'emission' : 'stroke', name: light ? '手绘发光' : '画笔', enabled: true,
      opacity: paintStroke.opacity, color: paintStroke.color, roughness: .65, metalness: .2,
      target: { scope: 'components', componentInstanceIds: [], assemblyWeaponIds: [state.project.activeWeaponId] },
      coverageMode: 'whole', coverage: {}, brush: { hardness: .7, shape: paintStroke.shape, stamps: [] } };
    if (light) layer.emission = { ...light };
    state.project.layers.push(layer); paintStroke.layer = layer;
  }
  const layer = paintStroke.layer;
  if (!layer.target.componentInstanceIds.includes(part.instanceId)) layer.target.componentInstanceIds.push(part.instanceId);
  if (part.role === 'ammo') {
    layer.target.ammoHosts = [part.hostEntityId];
    const bounds = new THREE.Box3().setFromBufferAttribute(mesh.geometry.attributes.position);
    layer.ammoSource = { unitId: part.unitId, componentId: part.componentId, instanceId: part.instanceId,
      bounds: [bounds.min.toArray(), bounds.max.toArray()], assembledTransform: clone(part.assembledTransform) };
  }
  layer.brush.stamps.push({ position: position.toArray(), normal: hit.face.normal.toArray(), radius,
    partId: part.instanceId, unitId: part.unitId });
  paintStroke.changed = true;
}

function drawBrushGuide(stroke, end) {
  const canvas = $('#brush-guide');
  if (!stroke) { canvas.hidden = true; return; }
  const rect = renderer.domElement.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width));
  canvas.height = Math.max(1, Math.round(rect.height));
  const context = canvas.getContext('2d');
  const points = brushPath(stroke.shape, stroke.start, end, 3, end.shiftKey);
  canvas.hidden = false;
  context.beginPath();
  points.forEach(({ clientX, clientY }, index) => {
    const x = clientX - rect.left, y = clientY - rect.top;
    if (index) context.lineTo(x, y);
    else context.moveTo(x, y);
  });
  context.lineWidth = 4; context.strokeStyle = '#fff'; context.stroke();
  context.lineWidth = 2; context.strokeStyle = stroke.color; context.stroke();
}

renderer.domElement.addEventListener('pointerdown', event => {
  if (!isBrushTool(state.tool) || event.button !== 0 || event.altKey || paintStroke) return;
  const shape = state.brushShapes[state.tool];
  const hit = hitTest(event);
  if (!hit && shape === 'freehand') return;
  const selected = state.project.viewState.selected.map(id => state.meshes.get(id)).filter(Boolean);
  const lightTool = state.tool === 'lightPaint';
  const whole = lightTool && state.project.viewState.paintScope === 'assembly' && !state.project.viewState.exploded;
  const ids = whole ? [] : selected.map(mesh => paintMesh(mesh).userData.part.instanceId);
  if (!whole && !ids.length && hit) ids.push(paintMesh(hit.object).userData.part.instanceId);
  const color = $(lightTool ? '#paint-light-color' : '#paint-color').value;
  const light = lightTool ? { ...DEFAULT_LIGHT, color, enabled: true } : null;
  event.preventDefault(); event.stopImmediatePropagation();
  renderer.domElement.focus({ preventScroll: true });
  colorBefore = clone(state.project);
  controls.enabled = false;
  renderer.domElement.setPointerCapture(event.pointerId);
  const start = { clientX: event.clientX, clientY: event.clientY };
  paintStroke = { pointerId: event.pointerId, ids, lockFirst: !whole && !ids.length, light,
    shape, color, opacity: Number($('#brush-opacity').value) / 100, start, lastPoint: start };
  if (shape === 'freehand') {
    paintSurface(event);
    refreshAppearance();
  } else drawBrushGuide(paintStroke, event);
}, true);
renderer.domElement.addEventListener('pointermove', event => {
  if (!paintStroke || event.pointerId !== paintStroke.pointerId) return;
  if (paintStroke.shape !== 'freehand') { drawBrushGuide(paintStroke, event); return; }
  const previous = paintStroke.lastPoint;
  const distance = Math.hypot(event.clientX - previous.clientX, event.clientY - previous.clientY);
  const steps = Math.max(1, Math.ceil(distance / Math.max(3, (state.brushRadiusPixels || 8) * .35)));
  for (let index = 1; index <= steps; index++) paintSurface({
    clientX: previous.clientX + (event.clientX - previous.clientX) * index / steps,
    clientY: previous.clientY + (event.clientY - previous.clientY) * index / steps
  });
  paintStroke.lastPoint = { clientX: event.clientX, clientY: event.clientY };
  refreshAppearance();
}, true);
function finishBrush(event) {
  if (!paintStroke || event?.pointerId !== undefined && event.pointerId !== paintStroke.pointerId) return;
  if (paintStroke.shape !== 'freehand' && event?.type === 'pointerup') {
    const samples = brushPath(paintStroke.shape, paintStroke.start, event,
      Math.max(2, (state.brushRadiusPixels || 8) * .28), event.shiftKey);
    for (const point of samples) paintSurface(point);
    if (paintStroke.changed) refreshAppearance();
  }
  const { changed, pointerId, layer, light } = paintStroke;
  paintStroke = null;
  drawBrushGuide(null);
  pointerStart = null;
  controls.enabled = true;
  if (renderer.domElement.hasPointerCapture(pointerId)) renderer.domElement.releasePointerCapture(pointerId);
  if (changed) {
    state.layerId = light && layer ? layer.layerId : null;
    finishColorGesture();
  }
  else colorBefore = null;
  renderLayers(); renderComponents(); renderEditor();
}
window.addEventListener('pointerup', finishBrush, true);
window.addEventListener('pointercancel', finishBrush, true);
renderer.domElement.addEventListener('lostpointercapture', finishBrush);

function releaseTemporaryBrush() {
  if (!temporaryBrush) return;
  const previous = temporaryBrush;
  temporaryBrush = null;
  finishBrush();
  state.layerId = previous.layerId;
  setTool(previous.tool);
  renderLayers(); renderEditor();
}
window.addEventListener('keyup', event => {
  if (event.code === 'KeyB' || event.key.toLowerCase() === 'b') releaseTemporaryBrush();
});
window.addEventListener('blur', () => { finishBrush(); releaseTemporaryBrush(); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { finishBrush(); releaseTemporaryBrush(); }
});
renderer.domElement.addEventListener('pointermove', event => {
  const cursor = $('#brush-cursor');
  const hit = isBrushTool(state.tool) && !event.altKey ? hitTest(event) : null;
  cursor.hidden = !hit;
  if (!hit) return;
  const rect = $('#viewport').getBoundingClientRect();
  const radius = THREE.MathUtils.clamp(Number($('#brush-size').value) || 8, .5, 60) / 1000;
  const pixels = radius / Math.max(hit.distance, .001) * rect.height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
  state.brushRadiusPixels = pixels;
  cursor.style.left = `${event.clientX - rect.left}px`; cursor.style.top = `${event.clientY - rect.top}px`;
  cursor.style.width = cursor.style.height = `${Math.max(2, pixels * 2)}px`;
});
renderer.domElement.addEventListener('pointerleave', () => { $('#brush-cursor').hidden = true; });
renderer.domElement.addEventListener('pointerdown', event => { pointerStart = { x: event.clientX, y: event.clientY, button: event.button, moved: false }; });
renderer.domElement.addEventListener('pointermove', event => {
  if (pointerStart && Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 5) {
    pointerStart.moved = true;
    closeContextMenu();
  }
});
renderer.domElement.addEventListener('pointerup', event => {
  const start = pointerStart;
  pointerStart = null;
  lastPointer = start;
  if (event.button !== 0 || !start || start.moved) return;
  try {
    const hit = hitTest(event);
    if (!hit) return;
    if (state.tool === 'select') selectHit(hit, event);
    else if (state.tool === 'place') placeHit(hit);
  } catch (error) { handleError(error); }
});
renderer.domElement.addEventListener('contextmenu', event => {
  event.preventDefault();
  if (lastPointer?.button === 2 && lastPointer.moved) return;
  const hit = hitTest(event);
  if (hit && !hitIsSelected(hit)) selectHit(hit, {});
  if (hit) state.lastHit = hit;
  const hasTarget = state.project.viewState.paintScope === 'assembly' || state.project.viewState.selected.length > 0;
  const items = [
    { label: '选择颜色', action: () => {
      if ($('#assembly-content').hidden) $('#assembly-toggle').click();
      $('#color-panel').parentElement.open = true;
      $('#paint-color').click();
    }, disabled: !hasTarget },
    { label: '给选区添加贴花', action: () => $('#import-image').click(), disabled: !hasTarget },
    { label: '给选区添加文字', action: () => $('#add-text').click(), disabled: !hasTarget },
    null
  ];
  if (hit) {
    const identity = hit.object.userData.part.instanceId;
    items.push({ label: '仅显示此部件', action: () => {
      state.project.viewState.selected = [identity];
      $('#isolate').click();
    } });
    items.push({ label: '隐藏此部件', action: () => commit(() => {
      state.project.viewState.hiddenParts = [...new Set([...(state.project.viewState.hiddenParts || []), identity])];
    }, { viewOnly: true, editor: false }) });
  }
  items.push({ label: '显示全部部件', action: () => $('#show-all').click() },
    { label: '清除选区', action: () => $('#clear-selection').click() },
    { label: '适应视图', action: () => fitView() });
  openContextMenu(event, items);
});
renderer.domElement.addEventListener('dragover', event => { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; });
renderer.domElement.addEventListener('drop', async event => {
  event.preventDefault();
  const point = { clientX: event.clientX, clientY: event.clientY };
  try {
    const hit = hitTest(point);
    if (!hit) throw new Error('请把图片拖到模型表面');
    if (!hitIsSelected(hit)) selectHit(hit, {});
    state.lastHit = hit;
    for (const file of [...event.dataTransfer.files].filter(item => item.type.startsWith('image/'))) {
      const image = await api('/api/import', { data: await fileData(file), name: file.name });
      addImageLayer(image);
    }
    state.library = await api('/api/library');
    renderLibrary();
  } catch (error) { handleError(error); }
});

async function fileData(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('文件读取失败'));
    reader.readAsDataURL(file);
  });
}

function renderLibrary() {
  $('#image-library').replaceChildren();
  for (const image of state.library) {
    const tile = document.createElement('button');
    tile.className = 'image-card';
    tile.dataset.imageHash = image.hash;
    tile.innerHTML = `<img src="/images/${image.hash}.png" alt=""><span>${escapeHTML(image.name)}</span>`;
    tile.title = image.name;
    tile.addEventListener('click', () => {
      try { addImageLayer(image); } catch (error) { handleError(error); }
    });
    tile.addEventListener('contextmenu', event => {
      event.preventDefault();
      const hasTarget = state.project.viewState.paintScope === 'assembly' || state.project.viewState.selected.length > 0;
      openContextMenu(event, [
        { label: '枪尾贴花', disabled: !hasTarget, action: () => addImageLayer(image, 'decal', 'end') },
        { label: '曲面环绕', disabled: !hasTarget, action: () => addImageLayer(image, 'decal', 'cylinder') },
        null,
        { label: '删除素材', danger: true, action: async () => {
        state.library = await api('/api/library/remove', { hash: image.hash });
        renderLibrary();
      } }]);
    });
    const remove = document.createElement('span');
    remove.className = 'image-remove'; remove.textContent = '×'; remove.setAttribute('role', 'button');
    remove.setAttribute('aria-label', '删除素材 ' + image.name); remove.tabIndex = 0;
    const removeImage = async event => {
      event.preventDefault(); event.stopPropagation();
      try { state.library = await api('/api/library/remove', { hash: image.hash }); renderLibrary(); }
      catch (error) { handleError(error); }
    };
    remove.addEventListener('click', removeImage);
    remove.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') removeImage(event); });
    tile.append(remove);
    $('#image-library').append(tile);
  }
  if (!state.library.length) $('#image-library').innerHTML = '<p class="empty">点击“导入素材”添加图片。</p>';
}

function hitIsSelected(hit) {
  return state.project.viewState.selected.includes(hit.object.userData.part.instanceId);
}

function layerBounds(layer) {
  const bounds = new THREE.Box3();
  for (const mesh of state.meshes.values()) {
    if (!mesh.userData.part.paintSourceId && applies({ ...layer, enabled: true }, mesh.userData.part)) {
      bounds.union(new THREE.Box3().setFromBufferAttribute(mesh.geometry.attributes.position).applyMatrix4(mesh.userData.assembled));
    }
  }
  return bounds;
}

function frontPlaneProjection(projection, bounds, preserveReach = false) {
  if (bounds.isEmpty()) return projection;
  const normal = new THREE.Vector3(...projection.normal).normalize();
  const origin = new THREE.Vector3(...projection.origin);
  const front = new THREE.Vector3(), rear = new THREE.Vector3();
  for (const axis of ['x', 'y', 'z']) {
    front[axis] = normal[axis] >= 0 ? bounds.max[axis] : bounds.min[axis];
    rear[axis] = normal[axis] >= 0 ? bounds.min[axis] : bounds.max[axis];
  }
  const shift = front.clone().sub(origin).dot(normal) + .00002;
  const depth = preserveReach ? projection.depth + shift : front.clone().sub(rear).dot(normal) + .00004;
  return { ...projection, origin: origin.addScaledVector(normal, shift).toArray(), normal: normal.toArray(),
    depth: Math.max(.0001, depth), depthMode: 'inward' };
}

const planeDirections = {
  front: { normal: [0, -1, 0], label: '右侧', opposite: 'back' },
  back: { normal: [0, 1, 0], label: '左侧', opposite: 'front' },
  rear: { normal: [-1, 0, 0], label: '枪尾', opposite: 'muzzle' },
  muzzle: { normal: [1, 0, 0], label: '枪口端', opposite: 'rear' },
  top: { normal: [0, 0, 1], label: '顶部', opposite: 'bottom', up: [0, 1, 0] },
  bottom: { normal: [0, 0, -1], label: '底部', opposite: 'top', up: [0, 1, 0] }
};

function planeSide(normal) {
  const direction = new THREE.Vector3(...normal).normalize();
  return Object.keys(planeDirections).find(side =>
    direction.dot(new THREE.Vector3(...planeDirections[side].normal)) > .99999) || 'view';
}

function isEndSide(side) {
  return ['rear', 'muzzle', 'top', 'bottom'].includes(side);
}

function lateralSide() {
  return camera.position.y - controls.target.y <= 0 ? 'front' : 'back';
}

function planeOrientation(side) {
  const direction = planeDirections[side];
  const normal = new THREE.Vector3(...direction.normal);
  const v = new THREE.Vector3(...(direction.up || [0, 0, 1]));
  return { side, normal, v, u: v.clone().cross(normal) };
}

function projectionMeshes(layer) {
  return [...state.meshes.values()].filter(mesh => !mesh.userData.part.paintSourceId &&
    applies({ ...layer, enabled: true }, mesh.userData.part));
}

function projectionPoints(meshes, layer) {
  const points = [];
  for (const mesh of meshes) {
    const geometry = layer ? appearance.geometry(mesh, layer) : mesh.geometry;
    const positions = geometry.attributes.position;
    for (let index = 0; positions && index < positions.count; index++) {
      points.push(new THREE.Vector3().fromBufferAttribute(positions, index).applyMatrix4(mesh.userData.assembled));
    }
    if (geometry !== mesh.geometry) geometry.dispose();
  }
  return points;
}


function cylinderProjection(meshes, points, axis) {
  const bounds = new THREE.Box3().setFromPoints(points);
  const origin = bounds.getCenter(new THREE.Vector3());
  const direction = new THREE.Vector3();
  if (axis === 'auto' && meshes.length === 1) {
    const mesh = meshes[0], size = mesh.userData.part.localBounds[1].map((value, i) => value - mesh.userData.part.localBounds[0][i]);
    direction.setComponent(size.indexOf(Math.max(...size)), 1).transformDirection(mesh.userData.assembled);
  } else {
    const size = bounds.getSize(new THREE.Vector3()).toArray();
    direction.setComponent(axis === 'auto' ? size.indexOf(Math.max(...size)) : Number(axis), 1);
  }
  const normal = planeOrientation(lateralSide()).normal;
  normal.addScaledVector(direction, -normal.dot(direction));
  if (normal.lengthSq() < 1e-8) normal.set(0, 0, 1).addScaledVector(direction, -direction.z);
  normal.normalize();
  const u = new THREE.Vector3().crossVectors(direction, normal).normalize();
  let minHeight = Infinity, maxHeight = -Infinity, radius = .0001;
  const offset = new THREE.Vector3();
  for (const point of points) {
    offset.copy(point).sub(origin);
    radius = Math.max(radius, Math.hypot(offset.dot(u), offset.dot(normal)));
    const height = offset.dot(direction);
    minHeight = Math.min(minHeight, height); maxHeight = Math.max(maxHeight, height);
  }
  origin.addScaledVector(direction, (minHeight + maxHeight) / 2);
  return { type: 'cylinder', origin: origin.toArray(), u: u.toArray(), v: direction.toArray(),
    normal: normal.toArray(), axis, radius, width: 2 * Math.PI * radius,
    height: Math.max(.001, maxHeight - minHeight + .0001), depth: radius + .001, cutoff: .02, wrap: true };
}

function selectionProjection(image, target, mode) {
  const visible = projectionMeshes({ ...target, weaponId: state.project.activeWeaponId }).filter(mesh => mesh.visible ||
    state.project.viewState.selected.some(id => state.meshes.get(id)?.userData.part.paintSourceId === mesh.userData.part.instanceId));
  const bounds = new THREE.Box3();
  for (const mesh of visible) {
    bounds.union(new THREE.Box3().setFromBufferAttribute(mesh.geometry.attributes.position).applyMatrix4(mesh.userData.assembled));
  }
  if (bounds.isEmpty()) throw new Error('所选部件当前不可见');
  const center = bounds.getCenter(new THREE.Vector3()), size = bounds.getSize(new THREE.Vector3());
  const { side, normal, u, v } = planeOrientation(mode === 'end' ? 'rear' : lateralSide());
  const extent = axis => Math.abs(axis.x) * size.x + Math.abs(axis.y) * size.y + Math.abs(axis.z) * size.z;
  const width = Math.max(mode === 'end' ? .001 : .006, Math.min(extent(u), extent(v) * image.width / image.height) * .88);
  const mesh = visible[0];
  const projection = mode === 'cylinder' ? cylinderProjection(visible, projectionPoints(visible), 'auto')
    : frontPlaneProjection({ type: 'plane', side, origin: center.toArray(), u: u.toArray(), v: v.toArray(),
    normal: normal.toArray(), width, height: width * image.height / image.width,
    depth: Math.max(.001, extent(normal) / 2 + .001), cutoff: .02, wrap: mode !== 'end' }, bounds);
  return { ...projection,
    anchor: { instanceId: mesh.userData.part.instanceId, localPosition: center.clone().applyMatrix4(mesh.userData.assembled.clone().invert()).toArray(),
      assembledTransform: clone(mesh.userData.part.assembledTransform) } };
}

function addImageLayer(image, kind = 'decal', mode = 'plane') {
  const target = targetForNewLayer();
  const projection = selectionProjection(image, target, mode);
  const anchor = projection.anchor;
  delete projection.anchor;
  newLayer(kind, {
    ...target,
    name: image.name, imageHash: image.hash, imageAspect: image.width / image.height, projection, anchor,
    placement: { x: 0, y: 0, angle: 0, scale: 1 }, crop: [0, 0, 1, 1]
  });
  setTool('select');
  renderComponents();
  renderSelectionGeometry();
  decalEditor.crop = false;
  toast(mode === 'cylinder' ? '拖动曲面上的图片，或在图层下方设置环绕范围' : '拖动图片移动，拖边角缩放，圆点旋转');
}

function setDecalProjection(type, axis = 'auto', side = null) {
  const layer = activeLayer();
  if (!layer?.imageHash) return;
  const meshes = projectionMeshes(layer);
  const points = projectionPoints(meshes, layer);
  if (!points.length) return;
  const bounds = new THREE.Box3().setFromPoints(points);
  const center = bounds.getCenter(new THREE.Vector3());
  let projection;
  if (type === 'cylinder') {
    projection = cylinderProjection(meshes, points, axis);
  } else {
    const aspect = layer.imageAspect || layer.projection.width / layer.projection.height;
    const currentSide = layer.projection.side;
    const direction = side || (type === 'end' ? (isEndSide(currentSide) ? currentSide : 'rear')
      : ['front', 'back'].includes(currentSide) ? currentSide : lateralSide());
    const { normal, u, v } = planeOrientation(direction);
    const horizontal = points.map(point => point.dot(u));
    const vertical = points.map(point => point.dot(v));
    const extent = values => values.reduce((maximum, value) => Math.max(maximum, value), -Infinity) - values.reduce((minimum, value) => Math.min(minimum, value), Infinity);
    const width = Math.max(.001, Math.min(extent(horizontal), extent(vertical) * aspect) * .88);
    projection = frontPlaneProjection({ type: 'plane', side: direction, origin: center.toArray(),
      u: u.toArray(), v: v.toArray(), normal: normal.toArray(), width, height: width / aspect,
      depth: .001, cutoff: .02, wrap: isEndSide(direction) ? false : layer.projection.wrap ?? true }, bounds);
  }
  commit(() => {
    layer.projection = projection;
    const mesh = meshes[0];
    layer.anchor = { instanceId: mesh.userData.part.instanceId, assembledTransform: clone(mesh.userData.part.assembledTransform),
      localPosition: center.clone().applyMatrix4(mesh.userData.assembled.clone().invert()).toArray() };
    Object.assign(layer.placement, { x: 0, y: 0, scale: 1, ...(type === 'cylinder' ? { angle: 0 } : {}) });
  });
}

function moveLayer(layer, direction) {
  commit(() => {
    const visible = state.project.layers.filter(layerInWorkspace);
    const next = visible[visible.indexOf(layer) + direction];
    if (!next) return;
    const first = state.project.layers.indexOf(layer);
    const second = state.project.layers.indexOf(next);
    [state.project.layers[first], state.project.layers[second]] = [state.project.layers[second], state.project.layers[first]];
  });
}

function closeContextMenu() {
  $('#context-menu').hidden = true;
}

function openContextMenu(event, items) {
  const menu = $('#context-menu');
  menu.replaceChildren();
  for (const item of items) {
    const element = document.createElement(item ? 'button' : 'div');
    if (!item) element.className = 'menu-separator';
    else {
      element.textContent = item.label;
      element.setAttribute('role', 'menuitem');
      element.disabled = Boolean(item.disabled);
      element.classList.toggle('danger', Boolean(item.danger));
      element.addEventListener('click', async () => {
        closeContextMenu();
        try { await item.action(); } catch (error) { handleError(error); }
      });
    }
    menu.append(element);
  }
  menu.hidden = false;
  menu.style.left = Math.max(6, Math.min(event.clientX, innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.style.top = Math.max(6, Math.min(event.clientY, innerHeight - menu.offsetHeight - 8)) + 'px';
}

document.addEventListener('pointerdown', event => { if (!$('#context-menu').contains(event.target)) closeContextMenu(); });
window.addEventListener('resize', closeContextMenu);

function duplicateLayer() {
  const original = activeLayer();
  if (!original) return;
  const duplicate = clone(original);
  duplicate.layerId = crypto.randomUUID();
  duplicate.name += ' 副本';
  commit(() => {
    const index = state.project.layers.indexOf(original);
    state.project.layers.splice(index + 1, 0, duplicate);
    state.layerId = duplicate.layerId;
  });
}

function oppositeSideLabel(layer) {
  const side = layer.projection.side || planeSide(layer.projection.normal);
  const opposite = layer.projection.type === 'cylinder' ? null : planeDirections[side]?.opposite;
  return '复制到' + (planeDirections[opposite]?.label || '对面');
}

function copyToOppositeSide() {
  const original = activeLayer();
  if (!original?.imageHash) return;
  const bounds = layerBounds(original);
  if (bounds.isEmpty()) throw new Error('请先装配此贴花所属的部件');
  const duplicate = clone(original);
  duplicate.layerId = crypto.randomUUID();
  duplicate.placement ||= { x: 0, y: 0, angle: 0, scale: 1 };
  const side = original.projection.side || planeSide(original.projection.normal);
  const destination = original.projection.type === 'cylinder' ? 'view' : planeDirections[side]?.opposite || 'view';
  const projection = duplicate.projection;
  const anchored = state.meshes.get(original.anchor?.instanceId);
  if (anchored && original.anchor.assembledTransform) {
    const delta = anchored.userData.assembled.clone()
      .multiply(new THREE.Matrix4().set(...original.anchor.assembledTransform.flat()).invert());
    projection.origin = new THREE.Vector3(...projection.origin).applyMatrix4(delta).toArray();
    const rotation = new THREE.Matrix3().setFromMatrix4(delta);
    for (const axis of ['u', 'v', 'normal']) {
      projection[axis] = new THREE.Vector3(...projection[axis]).applyMatrix3(rotation).toArray();
    }
  }
  if (projection.type === 'cylinder') {
    const halfTurn = Math.PI * projection.radius;
    duplicate.placement.x = THREE.MathUtils.euclideanModulo((duplicate.placement.x || 0) + 2 * halfTurn, 2 * halfTurn) - halfTurn;
  } else {
    const center = bounds.getCenter(new THREE.Vector3());
    const normal = new THREE.Vector3(...projection.normal).normalize();
    const origin = new THREE.Vector3(...projection.origin);
    projection.origin = origin.addScaledVector(normal, -2 * origin.clone().sub(center).dot(normal)).toArray();
    projection.normal = normal.negate().toArray();
    projection.u = projection.u.map(value => -value);
    projection.side = destination;
    duplicate.placement.x = -(duplicate.placement.x || 0);
    duplicate.placement.angle = -(duplicate.placement.angle || 0);
  }
  // The copy faces its own side. Its rectangle replaces any front-only quad selection.
  projection.wrap = false;
  duplicate.coverageMode = 'whole';
  duplicate.coverage = {};
  duplicate.coverageSources = {};
  const mesh = anchored || [...state.meshes.values()].find(item => applies({ ...original, enabled: true }, item.userData.part));
  duplicate.anchor = { instanceId: mesh.userData.part.instanceId,
    localPosition: new THREE.Vector3(...projection.origin).applyMatrix4(mesh.userData.assembled.clone().invert()).toArray(),
    assembledTransform: clone(mesh.userData.part.assembledTransform) };
  duplicate.name += ' ' + (planeDirections[destination]?.label || '对面') + '副本';
  commit(() => {
    state.project.layers.splice(state.project.layers.indexOf(original) + 1, 0, duplicate);
    state.layerId = duplicate.layerId;
  });
  decalEditor.crop = false;
  toast('已创建独立副本，可切换视角查看和编辑');
}

function deleteLayer() {
  const layer = activeLayer();
  if (!layer) return;
  commit(() => { state.project.layers.splice(state.project.layers.indexOf(layer), 1); state.layerId = null; });
}

async function saveProject() {
  $('#save-state').textContent = '正在封装工程';
  const snapshot = clone(state.project);
  const result = await api('/api/save', { project: snapshot });
  $('#save-state').textContent = '工程已保存';
  toast('工程已保存到 ' + result.path);
  return result;
}

async function openProject(data) {
  const project = await api('/api/open', data);
  state.project = project;
  await alignBuiltinAssets();
  state.layerId = null;
  state.undo = [];
  state.redo = [];
  state.category = activeWeapon().category;
  state.library = await api('/api/library');
  renderCatalog();
  renderLibrary();
  await loadAssembly(true);
  renderEditor();
  updateHistoryButtons();
  $('#open-dialog').close();
  toast('工程已恢复');
}

async function alignBuiltinAssets() {
  const builtin = await api('/api/catalog');
  const hash = Object.values(state.project.assetLocks)[0];
  const key = state.project.activePackKey || Object.keys(state.project.assetLocks)[0];
  if (hash !== builtin.packHash && key.startsWith('builtin.')) {
    const result = await api('/api/migrate', { project: state.project, targetPack: 'builtin', componentRemap: {} });
    state.project = result.project;
    state.catalog = builtin;
    await api('/api/autosave', { project: state.project });
    toast('已升级工程资产，来源工程已保存在迁移记录中');
  } else {
    state.catalog = hash === builtin.packHash ? builtin : await api('/api/catalog?packHash=' + hash);
  }
}

async function updateExportPlan() {
  const plan = await api('/api/export-plan', { project: state.project });
  const animated = Object.values(plan.animatedLayers);
  state.exportHasAnimation = animated.length > 0;
  let html = `<div class="export-weapons">${plan.weapons.map(weapon => `<div>${escapeHTML(weapon.label)}</div>`).join('')}</div>`;
  html += `<p class="export-path">保存位置<br>${escapeHTML(plan.outputDirectory)}</p>`;
  if (plan.weapons.length > 1) html += '<p>关联装备将一并打包。</p>';
  html += plan.issues.map(issue => `<p class="issue">${escapeHTML(issue.message)}</p>`).join('');
  if (animated.length && plan.canExport) html += `<p class="export-light-summary">将导出 ${animated.length} 个原生动画图层。进入任务后观察效果。</p>`;
  if (plan.selectedAttachments.length) {
    html += '<details><summary>游戏配件选择</summary><p>在游戏中选用与工作台一致的配件。</p><p>' +
      plan.selectedAttachments.map(row => escapeHTML(row.label)).join('、') + '</p></details>';
  }
  if (plan.ejectedCasings.length) html += '<p>弹药涂装将同步到游戏抛壳模型。</p>';
  if (plan.sharedImpact.length > 1) html += '<details><summary>共享资源使用范围</summary><p>外观替换会作用于使用同一资源的装备。</p><p>' + plan.sharedImpact.map(row => escapeHTML(row.label)).join('、') + '</p></details>';
  $('#export-plan').innerHTML = html;
  $('#start-export').disabled = !plan.canExport || Boolean(state.jobId);
  $('#export-include-project').disabled = Boolean(state.jobId);
  $('#export-name').disabled = Boolean(state.jobId);
  return plan;
}

async function capturePreview(steadyLighting = false) {
  $('.workspace').inert = true;
  $('.app-header').inert = true;
  try {
    return await captureFrozenPreview(steadyLighting);
  } finally {
    $('.workspace').inert = false;
    $('.app-header').inert = false;
  }
}

async function captureFrozenPreview(steadyLighting) {
  const snapshot = clone(state.project);
  await refreshAppearance();
  const originalCamera = camera.position.clone();
  const originalTarget = controls.target.clone();
  const originalDistance = displayDistance;
  const visibility = new Map([...state.meshes].map(([identity, mesh]) => [identity, mesh.visible]));
  const layers = [...(snapshot.colors || []), ...snapshot.layers];
  for (const mesh of state.meshes.values()) {
    const part = mesh.userData.part;
    mesh.visible = !part.detachedAmmoSample || layers.some(layer => applies(layer, part));
  }
  selections.visible = false;
  try {
    applyDisplayTransforms(0);
    const endDecal = snapshot.layers.find(layer => layer.enabled !== false && layer.imageHash &&
      layerInWorkspace(layer) && layer.projection.type !== 'cylinder' && Math.abs(layer.projection.normal[0]) > .75);
    const angle = endDecal ? Math.sign(endDecal.projection.normal[0]) * .9 : .16;
    fitView(new THREE.Vector3(angle, -1, .15).normalize(), false, false);
    setEnvironment('neutral');
    if (steadyLighting) appearance.animate(snapshot, state.meshes, elapsedSeconds, true);
    renderer.render(scene, camera);
    const canvas = document.createElement('canvas');
    canvas.width = 1400;
    canvas.height = 900;
    const context = canvas.getContext('2d');
    const gradient = context.createRadialGradient(700, 400, 20, 700, 420, 850);
    gradient.addColorStop(0, '#fffefa');
    gradient.addColorStop(1, '#e6e5e8');
    context.fillStyle = gradient;
    context.fillRect(0, 0, canvas.width, canvas.height);
    const source = renderer.domElement;
    const scale = Math.min(1280 / source.width, 710 / source.height);
    const width = source.width * scale;
    const height = source.height * scale;
    context.drawImage(source, (1400 - width) / 2, 90 + (710 - height) / 2, width, height);
    context.fillStyle = '#303645';
    context.font = '26px "Microsoft YaHei UI", sans-serif';
    context.fillText(state.assembly.linkedWeaponIds.map(id => state.catalog.weapons.find(item => item.weaponId === id).label).join('  ＋  '), 50, 55);
    context.fillStyle = '#535d73';
    context.font = '16px "Microsoft YaHei UI", sans-serif';
    context.fillText('离线工作台预览', 50, 855);
    return { project: snapshot, preview: canvas.toDataURL('image/png'), previewRevision: snapshot.revision };
  } finally {
    camera.position.copy(originalCamera);
    controls.target.copy(originalTarget);
    for (const [identity, visible] of visibility) state.meshes.get(identity).visible = visible;
    applyDisplayTransforms(originalDistance);
    selections.visible = true;
    setEnvironment();
    if (steadyLighting) appearance.animate(state.project, state.meshes, elapsedSeconds);
    renderer.render(scene, camera);
  }
}

async function pollJob() {
  if (!state.jobId) return;
  const job = await api('/api/export-status?jobId=' + state.jobId);
  $('#export-progress').hidden = false;
  $('#job-progress').value = job.progress || 0;
  $('#job-message').textContent = job.message;
  $('#job-elapsed').textContent = `已用 ${Math.floor(job.elapsedSeconds)} 秒`;
  $('#job-status').textContent = job.state === 'running' ? job.message : '';
  $('#cancel-export').hidden = job.state !== 'running';
  $('#export-include-project').disabled = job.state === 'running';
  if (job.state === 'complete') {
    $('#export-result').innerHTML = `<a href="/download/${encodeURI(job.result.relativePath)}" download>下载 MOD 安装包</a><p>${escapeHTML(job.result.path)}</p>`;
    $('#start-export').disabled = false;
    $('#export-name').disabled = false;
    toast('MOD 安装包已生成并通过独立回读');
    state.jobId = null;
  } else if (job.state !== 'running') {
    $('#export-result').textContent = job.message;
    $('#start-export').disabled = false;
    $('#export-name').disabled = false;
    state.jobId = null;
  } else setTimeout(() => pollJob().catch(handleError), 700);
}

const lightRegions = new LightRegionEditor($('#mask-dialog'), {
  saveMask: async (name, data) => (await api('/api/import', { name, purpose: 'mask', data })).hash,
  apply: (identity, regions, settings) => {
    const layer = state.project.layers.find(item => item.layerId === identity);
    if (!isArtwork(layer)) throw new Error('图片图层已变化，请重新打开');
    commit(() => {
      layer.emission = { ...settings, regions };
      delete layer.emission.maskHash;
    });
  },
  onOpen: renderLightEditor,
  onClose: renderLightEditor,
  onError: handleError
});

connect('#search', 'input', renderCatalog);
connect('#project-name', 'change', () => commit(() => {
  state.project.name = $('#project-name').value.trim() || '我的武器涂装';
}));
connect('#paint-color', 'input', () => {
  $('#paint-hex').value = $('#paint-color').value;
  if (state.tool !== 'paint' && (state.project.viewState.selected.length || state.project.viewState.paintScope === 'assembly')) previewColor(targetForNewLayer(), $('#paint-color').value);
});
connect('#paint-color', 'change', finishColorGesture);
connect('#paint-hex', 'change', () => {
  if (!/^#[0-9a-f]{6}$/i.test($('#paint-hex').value)) throw new Error('请输入 #RRGGBB 颜色');
  $('#paint-color').value = $('#paint-hex').value;
  if (state.tool !== 'paint' && (state.project.viewState.selected.length || state.project.viewState.paintScope === 'assembly')) applyColor();
});
connect('#brush-opacity', 'input', event => { $('#brush-opacity-value').value = event.target.value + '%'; });
connect('#brush-shape', 'change', event => {
  if (!isBrushTool(state.tool)) return;
  state.brushShapes[state.tool] = event.target.value;
  renderToolHint();
});
connect('#decal-depth', 'input', event => {
  const layer = activeLayer();
  const value = Number(event.target.value);
  if (!layer?.imageHash || !Number.isFinite(value) || value < .1 || value > 2000) return;
  commit(() => { editableLayer().projection.depth = value / 1000; }, { editor: false });
});
connect('#decal-fullwrap', 'change', event => {
  const layer = activeLayer();
  if (!layer?.imageHash) return;
  commit(() => { editableLayer().projection.wrap = event.target.checked; });
});
connect('#decal-projection', 'change', event => setDecalProjection(event.target.value));
connect('#decal-side', 'change', event => setDecalProjection(isEndSide(event.target.value) ? 'end' : 'plane', 'auto', event.target.value));
connect('#decal-axis', 'change', event => setDecalProjection('cylinder', event.target.value));
connect('#decal-angle', 'change', event => {
  const value = Number(event.target.value);
  if (activeLayer()?.imageHash && Number.isFinite(value)) commit(() => { editableLayer().placement.angle = value; });
});
connect('#decal-scale', 'change', event => {
  const value = Number(event.target.value);
  if (activeLayer()?.imageHash && value >= .01 && value <= 50) commit(() => { editableLayer().placement.scale = value; });
  else { renderEditor(); toast('缩放范围为 0.01 到 50'); }
});
for (const axis of ['x', 'y']) connect('#decal-' + axis, 'change', event => {
  const value = Number(event.target.value);
  if (activeLayer()?.imageHash && Number.isFinite(value)) commit(() => { editableLayer().placement[axis] = value / 1000; });
});
connect('#decal-wrap', 'change', event => {
  const value = Number(event.target.value);
  if (activeLayer()?.projection.type !== 'cylinder' || !(value > 0 && value <= 360)) return;
  commit(() => {
    const layer = editableLayer();
    layer.projection.width = THREE.MathUtils.degToRad(value) * layer.projection.radius / (layer.placement.scale || 1);
  });
});
connect('#decal-wrap-height', 'change', event => {
  const value = Number(event.target.value);
  if (activeLayer()?.projection.type !== 'cylinder' || !Number.isFinite(value) || value < .1) return;
  commit(() => {
    const layer = editableLayer();
    layer.projection.height = value / (1000 * (layer.placement.scale || 1));
  });
});
connect('#add-emission', 'click', () => {
  const target = targetForNewLayer();
  const settings = { ...DEFAULT_LIGHT, enabled: true, color: $('#paint-light-color').value };
  const layer = newLayer('emission', { ...target, name: '部件发光', color: settings.color, emission: settings });
  selectLayer(layer);
});
for (const [key, id] of Object.entries(lightFields)) connect('#' + id, 'change', event => writeLightControl(key, event.target));
connect('#emission-enabled', 'change', event => {
  lightRegions.settings.enabled = event.target.checked;
  renderLightEditor();
});
connect('#edit-light-regions', 'click', () => {
  const layer = activeDecal();
  if (layer) return lightRegions.open(layer, { ...lightSettings(layer), enabled: layer.emission?.enabled ?? true });
});
connect('#restore-color', 'click', () => commit(() => {
  const ids = state.project.viewState.selected.map(id => state.meshes.get(id)).filter(Boolean)
    .map(mesh => paintMesh(mesh).userData.part.instanceId);
  for (const layer of state.project.colors) {
    const restored = state.assembly.parts.filter(part => part.active && ids.includes(part.instanceId) && applies(layer, part)).map(part => part.instanceId);
    if (restored.length) layer.target.excludedComponentInstanceIds = [...new Set([...(layer.target.excludedComponentInstanceIds || []), ...restored])];
  }
  for (const layer of state.project.layers.filter(layer => layer.kind === 'stroke')) {
    layer.brush.stamps = layer.brush.stamps.filter(stamp => !ids.includes(stamp.partId));
    layer.target.componentInstanceIds = layer.target.componentInstanceIds.filter(id => !ids.includes(id));
    if (ids.some(id => state.meshes.get(id)?.userData.part.role === 'ammo')) layer.target.ammoHosts = [];
  }
  state.project.layers = state.project.layers.filter(layer => layer.kind !== 'stroke' || layer.brush.stamps.length);
}));
for (const button of document.querySelectorAll('[data-inspector]')) button.addEventListener('click', () => showInspector(button.dataset.inspector));
connect('#crop-image', 'click', () => { decalEditor.crop = !decalEditor.crop; });
connect('#copy-to-back', 'click', copyToOppositeSide);
connect('#finish-image', 'click', () => { state.layerId = null; decalEditor.crop = false; renderLayers(); renderEditor(); renderSelectionGeometry(); });

connect('#undo', 'click', () => restoreHistory('undo'));
connect('#redo', 'click', () => restoreHistory('redo'));
connect('#save-project', 'click', saveProject);
connect('#new-project', 'click', async () => {
  if (state.project.layers.length) await saveProject();
  state.project = await api('/api/new', {});
  state.catalog = await api('/api/catalog');
  state.layerId = null;
  state.undo = [];
  state.redo = [];
  state.category = 'primary';
  renderCatalog();
  await loadAssembly(true);
  renderEditor();
  scheduleSave();
});
connect('#open-project', 'click', async () => {
  const projects = await api('/api/projects');
  $('#saved-projects').replaceChildren();
  for (const project of projects.sort((a, b) => b.modified - a.modified)) {
    const button = document.createElement('button');
    button.className = 'project-item';
    button.textContent = project.name;
    button.addEventListener('click', () => openProject({ path: project.path }).catch(handleError));
    $('#saved-projects').append(button);
  }
  if (!projects.length) $('#saved-projects').innerHTML = '<p class="empty">保存的工程会显示在这里。</p>';
  $('#open-dialog').showModal();
});
connect('#browse-project', 'click', () => $('#project-input').click());
connect('#project-input', 'change', async event => {
  const file = event.target.files[0];
  if (file) await openProject({ data: await fileData(file) });
  event.target.value = '';
});
connect('#assembly-toggle', 'click', () => {
  const content = $('#assembly-content');
  content.hidden = !content.hidden;
  $('#assembly-toggle').setAttribute('aria-expanded', String(!content.hidden));
  $('#assembly-arrow').textContent = content.hidden ? '‹' : '›';
});
connect('#paint-scope', 'change', () => {
  state.project.viewState.paintScope = $('#paint-scope').value;
  if (state.project.viewState.paintScope === 'assembly') {
    state.project.viewState.selected = [];
    state.project.viewState.selectedQuads = {};
  }
  renderComponents();
  renderLightEditor();
  renderSelectionGeometry();
  scheduleSave();
});
connect('#isolate', 'click', () => {
  if (!state.project.viewState.selected.length) { toast('先选择需要保留显示的部件'); return; }
  commit(() => {
    const selected = new Set(state.project.viewState.selected);
    const hidden = new Set(state.project.viewState.hiddenParts || []);
    for (const identity of state.meshes.keys()) {
      if (selected.has(identity)) hidden.delete(identity);
      else hidden.add(identity);
    }
    state.project.viewState.hiddenParts = [...hidden];
  }, { viewOnly: true, editor: false });
  fitView();
});
connect('#show-all', 'click', () => commit(() => { state.project.viewState.hiddenParts = []; }, { viewOnly: true, editor: false }));
connect('#restore-selected', 'click', () => {
  const selected = state.project.viewState.selected.filter(id => state.meshes.has(id));
  if (!state.project.viewState.exploded || !selected.length) return;
  commit(() => {
    const restored = new Set(state.project.viewState.restoredParts || []);
    const separate = selected.every(id => restored.has(id));
    for (const identity of selected) {
      if (separate) restored.delete(identity);
      else restored.add(identity);
    }
    state.project.viewState.restoredParts = [...restored];
  }, { viewOnly: true, editor: false });
  applyDisplayTransforms();
});
connect('#explode', 'click', () => {
  state.undo.push(clone(state.project));
  state.redo = [];
  state.project.viewState.exploded = !state.project.viewState.exploded;
  state.project.viewState.restoredParts = [];
  state.project.viewState.paintScope = 'selection';
  if (state.project.viewState.exploded) state.project.viewState.distance = 1;
  state.project.revision += 1;
  animateDistance(smoothstep(state.project.viewState.exploded ? state.project.viewState.distance : 0));
  renderAssemblyControls();
  renderComponents(); renderSelectionGeometry();
  updateHistoryButtons();
  fitView(null, state.project.viewState.exploded);
  scheduleSave();
});
connect('#fit', 'click', () => fitView());
connect('#front', 'click', () => fitView(new THREE.Vector3(0, -1, 0)));
connect('#back', 'click', () => fitView(new THREE.Vector3(0, 1, 0)));
connect('#rear', 'click', () => fitView(new THREE.Vector3(-1, 0, 0)));
document.querySelectorAll('[data-tool]').forEach(button => button.addEventListener('click', () => {
  temporaryBrush = null;
  state.layerId = null;
  const tool = button.dataset.tool;
  setTool(isBrushTool(tool) && state.tool === tool ? 'select' : tool);
  renderLayers();
  renderEditor();
  renderSelectionGeometry();
}));
connect('#clear-selection', 'click', () => {
  state.project.viewState.paintScope = 'selection';
  state.lastHit = null;
  state.project.viewState.selectedQuads = {};
  state.project.viewState.selected = [];
  renderComponents(); renderSelectionGeometry(); scheduleSave();
});
for (const [button, mode] of [['import-image', 'plane'], ['import-rear', 'end'], ['import-cylinder', 'cylinder']]) {
  connect('#' + button, 'click', () => {
    $('#image-input').dataset.projectionMode = mode;
    $('#image-input').click();
  });
}
connect('#import-library', 'click', () => $('#library-input').click());
connect('#library-input', 'change', async event => {
  for (const file of event.target.files) await api('/api/import', { data: await fileData(file), name: file.name });
  state.library = await api('/api/library'); renderLibrary(); event.target.value = '';
});
connect('#image-input', 'change', async event => {
  const mode = event.target.dataset.projectionMode || 'plane';
  delete event.target.dataset.projectionMode;
  for (const file of event.target.files) {
    const image = await api('/api/import', { data: await fileData(file), name: file.name });
    addImageLayer(image, 'decal', mode);
  }
  state.library = await api('/api/library');
  renderLibrary();
  event.target.value = '';
});
connect('#add-text', 'click', () => $('#text-dialog').showModal());
connect('#text-color', 'input', () => { $('#text-hex').value = $('#text-color').value; });
connect('#text-hex', 'change', () => {
  if (!/^#[0-9a-f]{6}$/i.test($('#text-hex').value)) throw new Error('请输入 #RRGGBB 颜色');
  $('#text-color').value = $('#text-hex').value;
});
connect('#create-text', 'click', async () => {
  const image = await api('/api/text', { text: $('#text-content').value, color: $('#text-color').value });
  state.library = await api('/api/library');
  renderLibrary();
  addImageLayer(image, 'text');
  $('#text-dialog').close();
});
connect('#export', 'click', async () => {
  const name = $('#export-name');
  if (name.dataset.projectId !== state.project.projectId || !name.value) {
    name.dataset.projectId = state.project.projectId;
    name.value = state.project.name;
  }
  $('#export-dialog').showModal();
  await updateExportPlan();
});
connect('#start-export', 'click', async () => {
  const includeProject = $('#export-include-project').checked;
  const name = $('#export-name');
  const packageName = name.value.trim();
  if (!packageName) { name.focus(); throw new Error('请输入安装包名称'); }
  name.disabled = true;
  $('#export-include-project').disabled = true;
  $('#start-export').disabled = true;
  $('#export-result').textContent = '';
  $('#job-message').textContent = '正在生成当前工程的装配预览';
  $('#export-progress').hidden = false;
  try {
    const request = await capturePreview(state.exportHasAnimation);
    request.packageName = packageName;
    request.scope = 'current';
    request.includeProject = includeProject;
    const job = await api('/api/export', request);
    state.jobId = job.jobId;
    await pollJob();
  } catch (error) {
    name.disabled = false;
    $('#export-include-project').disabled = false;
    $('#start-export').disabled = false;
    $('#export-result').textContent = error.message;
    throw error;
  }
});
connect('#cancel-export', 'click', async () => {
  if (state.jobId) await api('/api/export-cancel', { jobId: state.jobId });
});
connect('#assets', 'click', async () => {
  const packs = await api('/api/asset-packs');
  $('#asset-info').innerHTML = packs.map(pack => `<h3>${escapeHTML(pack.manifest.packId)} / ${escapeHTML(pack.manifest.version)}</h3><p class="capability">${pack.manifest.units} 个原生 Unit · 构建 ${escapeHTML(pack.manifest.gameBuild)} · ${escapeHTML(pack.manifest.validation || '原生映射与工作台')}</p>`).join('');
  $('#assets-dialog').showModal();
});
connect('#import-pack', 'click', () => $('#pack-input').click());
connect('#pack-input', 'change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  const result = await api('/api/assets/import', { data: await fileData(file) });
  state.importedPack = result.id;
  state.catalog = await api('/api/catalog');
  $('#migration-section').hidden = false;
  $('#migration-report').textContent = JSON.stringify(result, null, 2);
  renderCatalog();
  event.target.value = '';
});
connect('#migrate-project', 'click', async () => {
  const result = await api('/api/migrate', { project: state.project, targetPack: state.importedPack, componentRemap: JSON.parse($('#component-remap').value) });
  await saveProject();
  state.project = result.project;
  state.catalog = await api('/api/catalog?packHash=' + Object.values(state.project.assetLocks)[0]);
  renderCatalog();
  $('#migration-report').textContent = JSON.stringify(result.report, null, 2);
  await api('/api/save', { project: state.project });
  await loadAssembly(true);
  toast('迁移工程已另存为新版本');
});

window.addEventListener('keydown', event => {
  if (event.key === 'Escape') closeContextMenu();
  const editing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName);
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); saveProject().catch(handleError); return; }
  if (document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape') {
    temporaryBrush = null;
    state.layerId = null;
    setTool('select'); renderLayers(); renderEditor();
    return;
  }
  if (editing) return;
  if (event.ctrlKey && event.key.toLowerCase() === 'z') { event.preventDefault(); finishBrush(); restoreHistory(event.shiftKey ? 'redo' : 'undo').catch(handleError); }
  if (event.ctrlKey && event.key.toLowerCase() === 'y') { event.preventDefault(); finishBrush(); restoreHistory('redo').catch(handleError); }
  if (event.ctrlKey && event.key.toLowerCase() === 'd') { event.preventDefault(); duplicateLayer(); }
  if (event.key === 'Delete') deleteLayer();
  if (event.key.toLowerCase() === 'f') fitView();
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key.toLowerCase() === 'b' && !event.repeat && !temporaryBrush) {
    event.preventDefault();
    temporaryBrush = { tool: state.tool, layerId: state.layerId };
    state.layerId = null; setTool('paint'); renderLayers(); renderEditor();
  }
  if (event.key.toLowerCase() === 'v') { temporaryBrush = null; state.layerId = null; setTool('select'); renderEditor(); }
});

window.studio = {
  getModel: () => modelRoot,
  getProject: () => clone(state.project),
  getAssembly: () => clone(state.assembly),
  capturePreview,
  requestClose: async () => {
    try {
      clearTimeout(saveTimer);
      if (state.project) await api('/api/autosave', { project: state.project });
      window.chrome?.webview?.postMessage('studio-save-layout');
      window.chrome?.webview?.postMessage('studio-close-approved');
    } catch (error) { handleError(error); }
  }
};

connect('#choose-stock', 'click', () => {
  if (!window.chrome?.webview) throw new Error('请在桌面程序中选择游戏目录');
  window.chrome.webview.postMessage('stock-choose-folder');
});
for (const name of ['bilibili', 'github']) {
  connect('#author-' + name, 'click', event => {
    if (!window.chrome?.webview) return;
    event.preventDefault();
    window.chrome.webview.postMessage('author-open-' + name);
  });
}
window.chrome?.webview?.addEventListener('message', async event => {
  if (event.data?.type === 'author-link-error') {
    toast(event.data.message, true);
    return;
  }
  if (event.data?.type !== 'stock-folder') return;
  const button = $('#choose-stock'), message = $('#stock-status');
  button.disabled = true;
  message.textContent = '正在准备本机资源…';
  try {
    await api('/api/prepare-stock', { gameDirectory: event.data.path });
    location.reload();
  } catch (error) {
    message.textContent = error.message;
    button.disabled = false;
  }
});

async function start() {
  if (!(await api('/api/stock-status')).ready) {
    $('.workspace').hidden = true;
    $('#stock-screen').hidden = false;
    $('#status').textContent = '等待准备游戏资源';
    window.__studioReady = true;
    return;
  }
  [state.catalog, state.project, state.library] = await Promise.all([api('/api/catalog'), api('/api/recovery'), api('/api/library')]);
  await alignBuiltinAssets();
  state.category = activeWeapon().category;
  state.project.viewState.selectedQuads ||= {};
  state.project.viewState.paintScope ||= 'selection';
  state.project.colors ||= [];
  if (state.project.viewState.interactionVersion !== 3) {
    Object.assign(state.project.viewState, { selected: [], selectedQuads: {}, selectionMode: 'component', paintScope: 'selection', interactionVersion: 3 });
  }
  state.project.viewState.selectionMode = 'component'; state.project.viewState.selectedQuads = {};
  renderCatalog();
  renderLibrary();
  renderEditor();
  updateHistoryButtons();
  await loadAssembly(true);
  $('#save-state').textContent = state.project.revision ? '已恢复工作台' : '新工程';
  const job = await api('/api/export-status');
  if (job?.state === 'running') { state.jobId = job.jobId; pollJob().catch(handleError); }
}

start().catch(error => {
  window.__startupError = error.message;
  $('#loading-text').textContent = error.message;
  handleError(error);
});
